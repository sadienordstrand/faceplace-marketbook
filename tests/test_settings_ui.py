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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scheduling as sc
import settings_ui

try:
    from playwright.sync_api import sync_playwright  # noqa: F401
    HAVE_PLAYWRIGHT = True
except ImportError:
    HAVE_PLAYWRIGHT = False

CITIES = ["Medford, OR", "Sacramento, CA", "Denver, CO"]
PACES = {"fast": (1.0, 2.5), "slow": (3.0, 5.0)}


@unittest.skipUnless(HAVE_PLAYWRIGHT, "needs Playwright")
class UITest(unittest.TestCase):
    """Each test supplies a script that drives the page; the window closes when
    the script returns."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self._saved = {k: getattr(sc, k) for k in
                       ("SEARCHES_PATH", "EMAIL_CONFIG_PATH", "STATE_DIR",
                        "LOCK_PATH", "TICK_LOG")}
        sc.SEARCHES_PATH = root / "saved_searches.json"
        sc.EMAIL_CONFIG_PATH = root / "email_config.json"
        sc.STATE_DIR = root / ".schedule"
        sc.LOCK_PATH = sc.STATE_DIR / "run.lock"
        sc.TICK_LOG = sc.STATE_DIR / "tick.log"
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

    def drive(self, script, defaults=None, cities=None, builtins=()):
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
            headless=True, hooks=sc.ui_hooks(),
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
            page.click("#noCities")
            page.click(".cities .tog[data-city='Denver, CO']")
            page.click("#start")
            page.wait_for_timeout(300)
        data = self.drive(script)
        self.assertEqual(data["action"], "sweep")
        self.assertEqual(data["query"], "defender 110")
        self.assertEqual(data["cities"], ["Denver, CO"])
        self.assertEqual(data["max_price"], 40000)
        self.assertEqual(data["exclude"], "rhd, can am")

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
            self.assertIn("permanent", page.text_content("#addCityMsg"))
        self.drive(script, cities=["Medford, OR", "Denver, CO"],
                   builtins=["Medford, OR"])

    def test_a_city_the_user_added_takes_two_clicks_to_remove(self):
        def script(page):
            x = '.tog[data-city="Denver, CO"] .tog-x'
            page.click(x)
            self.assertEqual(page.text_content(x), "Remove?")
            self.assertIsNotNone(page.query_selector('.tog[data-city="Denver, CO"]'))
            page.click(x)
            page.wait_for_selector('.tog[data-city="Denver, CO"]', state="detached")
            self.assertIn("Removed Denver, CO", page.text_content("#addCityMsg"))
        self.drive(script, cities=["Medford, OR", "Denver, CO"],
                   builtins=["Medford, OR"])

    def test_a_refused_removal_keeps_the_city_and_says_why(self):
        # The button is hidden for built-ins, so this can only be reached by a
        # stale page; it still must not drop the city from the list.
        def script(page):
            page.eval_on_selector(
                '.tog[data-city="Medford, OR"]',
                "el => el.insertAdjacentHTML('beforeend',"
                " '<button class=\"tog-x\">x</button>')")
            x = '.tog[data-city="Medford, OR"] .tog-x'
            page.click(x)
            page.click(x)
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


if __name__ == "__main__":
    unittest.main()
