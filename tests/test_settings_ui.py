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
        self.addCleanup(self._restore)
        self.errors = []

    def _restore(self):
        sc.schedule_installed = self._installed
        sc.install_schedule = self._install
        sc.rearm_wake = self._wake
        for k, v in self._saved.items():
            setattr(sc, k, v)

    def drive(self, script, defaults=None, cities=None, builtins=(),
              extra_hooks=None):
        """Opens the window, runs `script(page)`, returns what was submitted."""
        result = {}
        cities = list(cities or CITIES)

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
                         "page_work": 3.5, "photo_save": 1.5,
                         "descriptions_budget": 0},
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
            self.assertIn("No saved searches yet", page.text_content("#savedList"))
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
            self.assertEqual(page.text_content("#saveSearch"), "Update saved search")
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
        self.assertEqual(data["query"], "defender 110")
        self.assertEqual(data["cities"], ["Denver, CO"])
        self.assertEqual(data["max_price"], 40000)
        self.assertEqual(data["min_year"], 1970)
        self.assertEqual(data["max_year"], 1995)
        self.assertEqual(data["exclude"], "rhd, can am")

    def test_undated_listings_are_included_unless_asked_otherwise(self):
        def script(page):
            page.fill("#query", "defender 110")
            self.assertEqual(
                page.get_attribute("#include_no_year", "aria-pressed"), "true")
            page.click("#include_no_year")
            page.click("#start")
            page.wait_for_timeout(300)
        self.assertIs(self.drive(script)["include_no_year"], False)

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
                page.fill(lo_id, lo)
                page.fill(hi_id, hi)
                self.assertFalse(page.is_hidden("#filterMsg"), expected)
                self.assertIn(expected, page.text_content("#filterMsg").lower())
                self.assertTrue(page.is_disabled("#start"), expected)
                self.assertTrue(page.is_disabled("#saveSearch"), expected)
                self.assertIn("check the filters", page.text_content("#est"))
                page.fill(lo_id, "")
                page.fill(hi_id, "")
            # and clearing the last of them puts the button back
            self.assertTrue(page.is_hidden("#filterMsg"))
            self.assertFalse(page.is_disabled("#start"))
        self.drive(script)

    def test_start_is_disabled_without_a_query_or_a_city(self):
        def script(page):
            self.assertTrue(page.is_disabled("#start"))
            page.fill("#query", "x")
            self.assertFalse(page.is_disabled("#start"))
            page.click("#noCities")
            self.assertTrue(page.is_disabled("#start"))
        self.drive(script)

    def test_the_email_tab_saves_settings(self):
        def script(page):
            page.click("#tabEmail")
            page.fill("#mail_address", "me@gmail.com")
            page.fill("#mail_password", "abcd efgh ijkl mnop")
            page.click("#saveMail")
            page.wait_for_selector("#mailMsg:not([hidden])")
            self.assertIn("me@gmail.com", page.text_content("#mailMsg"))
        self.drive(script)
        cfg = json.loads(sc.EMAIL_CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(cfg["address"], "me@gmail.com")
        self.assertEqual(cfg["app_password"], "abcd efgh ijkl mnop")
        self.assertEqual(cfg["default_to"], "me@gmail.com")

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
                              "default_to": "someone@else.com"})

        def script(page):
            page.click("#tabEmail")
            self.assertEqual(page.input_value("#mail_address"), "prior@gmail.com")
            self.assertEqual(page.input_value("#mail_to"), "someone@else.com")
        self.drive(script)

    def test_a_test_send_without_a_password_explains_itself(self):
        def script(page):
            page.click("#tabEmail")
            page.click("#testMail")
            page.wait_for_selector("#mailMsg:not([hidden])")
            self.assertIn("app password", page.text_content("#mailMsg"))
        self.drive(script)

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
            self.assertIn("Removed Denver, CO", page.text_content("#addCityMsg"))
        self.drive(script, cities=["Medford, OR", "Denver, CO"],
                   builtins=["Medford, OR"])

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
                "() => document.getElementById('addCityMsg')"
                ".textContent.includes(\"can't be removed\")")
            self.assertIsNotNone(page.query_selector('.tog[data-city="Medford, OR"]'))
        self.drive(script, cities=["Medford, OR", "Denver, CO"],
                   builtins=["Medford, OR"])

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

    # ------------------------------------------------ the shortcut offer
    def shortcut_hooks(self, offer=None, result=None, reopen=None):
        """Stands in for make_desktop_icon, which would otherwise put a real
        icon on the desktop of whoever ran the tests."""
        self.asked = []
        return {
            "shortcut_offer": lambda: offer if offer is not None else OFFER,
            "shortcut_reopen": lambda: (
                self.asked.append(("reopen",))
                or (reopen if reopen is not None else OFFER)),
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

    def test_the_button_opens_the_sheet_when_the_launch_offer_stayed_quiet(self):
        # Replaces the Add to Desktop launchers: dismissing the offer, or already
        # having an icon, must not be the end of ever getting one.
        def script(page):
            self.assertTrue(page.is_hidden("#shortcutAsk"))
            page.click("#tabEmail")
            page.click("#shortcutOpen")
            page.wait_for_selector("#shortcutAsk:visible")
            self.assertEqual(
                page.eval_on_selector_all("#shortcutPlaces .tog",
                                          "els => els.map(e => e.dataset.place)"),
                ["desktop", "dock"])
        self.drive(script, extra_hooks=self.shortcut_hooks(offer={"ask": False}))
        self.assertEqual(self.asked, [("reopen",)])

    def test_a_machine_with_nowhere_to_put_one_says_so_rather_than_opening(self):
        def script(page):
            page.click("#tabEmail")
            page.click("#shortcutOpen")
            page.wait_for_selector("#shortcutOpenMsg:not([hidden])")
            self.assertIn("hasn't anywhere", page.text_content("#shortcutOpenMsg"))
            self.assertTrue(page.is_hidden("#shortcutAsk"))
        self.drive(script, extra_hooks=self.shortcut_hooks(
            offer={"ask": False}, reopen={"ask": False}))

    def test_reopening_after_an_add_starts_the_sheet_over(self):
        # A sheet that's been through an add has its places and buttons put away.
        def script(page):
            page.wait_for_selector("#shortcutAsk:visible")
            page.click("#shortcutAdd")
            page.wait_for_selector("#shortcutMsg:not([hidden])")
            page.click("#shortcutSkip")
            page.wait_for_selector("#shortcutAsk", state="hidden")
            page.click("#tabEmail")
            page.click("#shortcutOpen")
            page.wait_for_selector("#shortcutAsk:visible")
            for back in ("#shortcutPlaces", "#shortcutNever", "#shortcutAdd"):
                self.assertFalse(page.is_hidden(back), back)
            self.assertTrue(page.is_hidden("#shortcutMsg"))
            self.assertEqual(page.text_content("#shortcutAdd").strip(),
                             "Add shortcut")
            self.assertEqual(page.text_content("#shortcutSkip").strip(), "Not now")
            self.assertEqual(
                page.get_attribute("#shortcutNever", "aria-pressed"), "false")
        self.drive(script, extra_hooks=self.shortcut_hooks())

    def test_closing_the_sheet_hands_focus_back_to_the_button_that_opened_it(self):
        def script(page):
            page.click("#tabEmail")
            page.click("#shortcutOpen")
            page.wait_for_selector("#shortcutAsk:visible")
            page.click("#shortcutSkip")
            page.wait_for_selector("#shortcutAsk", state="hidden")
            self.assertEqual(page.evaluate("document.activeElement.id"),
                             "shortcutOpen")
        self.drive(script, extra_hooks=self.shortcut_hooks(offer={"ask": False}))

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
        self.assertEqual((data or {}).get("query"), "defender 110")

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
            page.wait_for_timeout(300)
            page.click("#tabNew")
            page.fill("#query", "x")
            page.fill("#save_name", "Nope")
            page.click("#saveSearch")
            page.wait_for_selector("#saveMsg:not([hidden])")
            self.assertIn("isn't available", page.text_content("#saveMsg"))

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
