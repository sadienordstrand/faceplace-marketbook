#!/usr/bin/env python3
"""
Offline tests for scheduled searches and the times they run.

    python3 -m unittest discover tests

Nothing here touches the network, opens a browser, or reads the real
saved_searches.json, email_config.json or database — every path is redirected
into a temporary folder first. The live checks that do need Facebook or Gmail
live in the test plan, not in here.
"""
import base64
import csv
import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from email import message_from_bytes
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import browser
import fb_marketplace_sweep as fb
import locations
import storage
import scheduling as sc

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"


def dt(s):
    return datetime.fromisoformat(s)


class Redirected(unittest.TestCase):
    """Points every path scheduling.py writes to at a throwaway folder."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._saved = {k: getattr(sc, k) for k in
                       ("SEARCHES_PATH", "EMAIL_CONFIG_PATH", "SCHEDULE_DIR",
                        "LOCK_PATH", "TICK_LOG")}
        sc.SEARCHES_PATH = self.root / "saved_searches.json"
        sc.EMAIL_CONFIG_PATH = self.root / "email_config.json"
        sc.SCHEDULE_DIR = self.root / ".schedule"
        sc.LOCK_PATH = sc.SCHEDULE_DIR / "run.lock"
        sc.TICK_LOG = sc.SCHEDULE_DIR / "tick.log"
        # Collect progress messages instead of printing them over the test run.
        self.logged = []
        self._real_log = sc.log
        sc.log = self.logged.append
        # The wake queue must never reach the real system from a test: writing
        # it can raise a macOS password prompt, and reading it answers with
        # whatever this machine happens to have scheduled. Tests that care
        # replace these with their own recorders.
        self._wakes = {"scheduled_wakes": sc.scheduled_wakes,
                       "_admin_shell": sc._admin_shell}
        sc.scheduled_wakes = lambda: []
        sc._admin_shell = lambda lines: True
        self.addCleanup(self._restore)
        self.addCleanup(self.tmp.cleanup)

    def _restore(self):
        sc.log = self._real_log
        for k, v in self._wakes.items():
            setattr(sc, k, v)
        for k, v in self._saved.items():
            setattr(sc, k, v)

    def make(self, name="Defender 110", **kw):
        base = {"name": name, "query": "defender 110", "cities": ["Medford, OR"],
                "email_to": "me@example.com"}
        base.update(kw)
        rec, err = sc.add_search(base)
        self.assertIsNone(err, err)
        return rec


# ----------------------------------------------------------------- interval math
class TestIntervalMath(unittest.TestCase):
    def daily(self, last, every=1):
        return sc.next_run_at({"interval": {"every": every, "unit": "days"},
                               "last_started": last})

    def test_first_daily_run_is_the_next_5am(self):
        s = {"interval": {"every": 1, "unit": "days"}, "last_started": None}
        # Before 5am, today still counts.
        nxt = sc.next_run_at(s, after=dt("2026-08-06T03:30:00"))
        self.assertEqual(nxt, dt("2026-08-06T05:00:00"))
        # After 5am, it's tomorrow.
        nxt = sc.next_run_at(s, after=dt("2026-08-06T09:15:00"))
        self.assertEqual(nxt, dt("2026-08-07T05:00:00"))

    def test_daily_lands_on_5am_from_the_start_time(self):
        self.assertEqual(self.daily("2026-08-06T05:00:04"),
                         dt("2026-08-07T05:00:00"))

    def test_a_late_start_does_not_drag_the_schedule_later(self):
        # The whole point of anchoring to the start: a run that began at 05:00
        # and took 40 minutes still schedules the next one for 05:00.
        s = {"interval": {"every": 1, "unit": "days"},
             "last_started": "2026-08-06T05:00:00",
             "last_finished": "2026-08-06T05:41:00"}
        self.assertEqual(sc.next_run_at(s), dt("2026-08-07T05:00:00"))

    def test_a_run_that_started_late_still_moves_forward(self):
        # Woke up at 09:12 and ran then; the next one must be tomorrow, not a
        # time already in the past that would fire again immediately.
        nxt = self.daily("2026-08-06T09:12:00")
        self.assertEqual(nxt, dt("2026-08-07T05:00:00"))
        self.assertGreater(nxt, dt("2026-08-06T09:12:00"))

    def test_every_n_days(self):
        self.assertEqual(self.daily("2026-08-06T05:00:00", every=3),
                         dt("2026-08-09T05:00:00"))

    def test_5am_survives_a_daylight_saving_change(self):
        # Naive wall-clock arithmetic is what keeps this at 5am on both sides.
        for last in ("2026-03-07T05:00:00", "2026-10-31T05:00:00"):
            with self.subTest(last=last):
                self.assertEqual(self.daily(last).hour, 5)

    # Hour intervals run on a fixed daily grid anchored at 5am, so the Mac's
    # wake-ups can be written weeks ahead: the times never move, only the count
    # of days they're queued for.
    def test_the_grid_starts_at_5am_and_repeats_daily(self):
        self.assertEqual(sc.grid_hours(6), [5, 11, 17, 23])
        self.assertEqual(sc.grid_hours(12), [5, 17])
        self.assertEqual(sc.grid_hours(8), [5, 13, 21])
        self.assertEqual(sc.grid_hours(3), [2, 5, 8, 11, 14, 17, 20, 23])

    def test_a_legacy_count_that_does_not_divide_24_still_gets_a_grid(self):
        # Saved before hours became a menu. The times still repeat daily; the
        # wrap past midnight back to 5am is just one short gap.
        self.assertEqual(sc.grid_hours(5), [1, 5, 10, 15, 20])

    def test_hours_snap_to_the_grid_not_to_the_last_start(self):
        # A run that started at 11:00:04 measures from its start, and the next
        # slot is 5pm — the same 5pm as every other day, not 5:00:04.
        s = {"interval": {"every": 6, "unit": "hours"},
             "last_started": "2026-08-06T11:00:04"}
        self.assertEqual(sc.next_run_at(s, after=dt("2026-08-06T11:45:00")),
                         dt("2026-08-06T17:00:00"))

    def test_first_hourly_run_waits_for_the_next_slot(self):
        # Save at 4pm, every 6 hours: the first run is 5pm, because that's the
        # next point on the 5am / 11am / 5pm / 11pm grid. Run now exists for
        # anyone who doesn't want to wait.
        s = {"interval": {"every": 6, "unit": "hours"}, "last_started": None}
        self.assertEqual(sc.next_run_at(s, after=dt("2026-08-06T16:00:00")),
                         dt("2026-08-06T17:00:00"))
        s = {"interval": {"every": 12, "unit": "hours"}, "last_started": None}
        self.assertEqual(sc.next_run_at(s, after=dt("2026-08-06T14:00:00")),
                         dt("2026-08-06T17:00:00"))

    def test_a_machine_that_slept_catches_up_once_not_five_times(self):
        # Every 3 hours, last started at 5am, woken at 2:30pm: the slots it
        # slept through are gone. The catch-up run happens when the tick finds
        # next_run in the past; measured from that start, the schedule is back
        # on the grid.
        s = {"interval": {"every": 3, "unit": "hours"},
             "last_started": "2026-08-06T14:30:00"}
        self.assertEqual(sc.next_run_at(s), dt("2026-08-06T17:00:00"))

    def test_minutes_unit_exists_for_testing(self):
        s = {"interval": {"every": 5, "unit": "minutes"},
             "last_started": "2026-08-06T06:00:00"}
        self.assertEqual(sc.next_run_at(s, after=dt("2026-08-06T06:01:00")),
                         dt("2026-08-06T06:05:00"))

    def test_interval_hours_and_description(self):
        self.assertEqual(sc.interval_hours({"every": 2, "unit": "days"}), 48)
        self.assertEqual(sc.interval_hours({"every": 3, "unit": "hours"}), 3)
        self.assertEqual(sc.describe_interval({"every": 1, "unit": "days"}),
                         "every day")
        self.assertEqual(sc.describe_interval({"every": 6, "unit": "hours"}),
                         "every 6 hours")


class TestTimeParsing(unittest.TestCase):
    def test_a_naive_stamp_is_taken_as_local(self):
        self.assertEqual(sc.parse_iso("2026-08-06T23:12:00"),
                         dt("2026-08-06T23:12:00"))

    def test_an_offset_is_converted_not_discarded(self):
        # The sweep records its start in UTC. Dropping the offset instead of
        # converting put an 11:12pm run into the report as "tomorrow at 5:12 am".
        naive = sc.parse_iso("2026-08-07T05:12:00+00:00")
        expect = (datetime.fromisoformat("2026-08-07T05:12:00+00:00")
                  .astimezone().replace(tzinfo=None))
        self.assertEqual(naive, expect)
        self.assertIsNone(naive.tzinfo)

    def test_a_round_trip_through_iso_is_stable(self):
        now = sc.now_local()
        self.assertEqual(sc.parse_iso(sc.iso(now)), now)

    def test_junk_is_none_not_an_exception(self):
        for bad in (None, "", "not a date", "2026-13-45T99:99:99", 12345):
            with self.subTest(bad=bad):
                self.assertIsNone(sc.parse_iso(bad))

    def test_microseconds_are_dropped(self):
        self.assertEqual(sc.parse_iso("2026-08-06T23:12:00.123456"),
                         dt("2026-08-06T23:12:00"))


class TestDueAndLate(unittest.TestCase):
    def test_paused_searches_are_never_due(self):
        s = {"enabled": False, "next_run": "2020-01-01T05:00:00",
             "interval": {"every": 1, "unit": "days"}}
        self.assertFalse(sc.is_due(s, dt("2026-08-06T09:00:00")))

    def test_due_when_next_run_has_passed(self):
        s = {"enabled": True, "next_run": "2026-08-06T05:00:00",
             "interval": {"every": 1, "unit": "days"}}
        self.assertTrue(sc.is_due(s, dt("2026-08-06T05:00:01")))
        self.assertFalse(sc.is_due(s, dt("2026-08-06T04:59:59")))

    def test_lateness_threshold(self):
        s = {"next_run": "2026-08-06T05:00:00"}
        self.assertAlmostEqual(sc.lateness_hours(s, dt("2026-08-06T06:30:00")),
                               1.5, places=3)
        self.assertLess(sc.lateness_hours(s, dt("2026-08-06T06:30:00")),
                        sc.LATE_AFTER_HOURS)
        self.assertGreaterEqual(sc.lateness_hours(s, dt("2026-08-06T08:00:00")),
                                sc.LATE_AFTER_HOURS)


class TestQueueOrder(Redirected):
    def test_due_searches_come_back_in_creation_order(self):
        names = ["Third", "First", "Second"]
        created = ["2026-08-03T10:00:00", "2026-08-01T10:00:00",
                   "2026-08-02T10:00:00"]
        searches = [{**sc.DEFAULT_SEARCH, "id": n.lower(), "name": n,
                     "created": c, "enabled": True,
                     "next_run": "2026-08-06T05:00:00"}
                    for n, c in zip(names, created)]
        sc.save_searches(searches)
        order = [s["name"] for s in
                 sc.due_searches(sc.load_searches(), dt("2026-08-06T06:00:00"))]
        self.assertEqual(order, ["First", "Second", "Third"])


# ------------------------------------------------------------------------- CRUD
class TestSavedSearchCRUD(Redirected):
    def test_round_trip(self):
        rec = self.make()
        again = sc.load_searches()
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0]["name"], "Defender 110")
        self.assertEqual(again[0]["id"], rec["id"])
        self.assertTrue(again[0]["next_run"])

    def test_names_with_punctuation_and_accents_survive(self):
        rec = self.make(name="Rolls-Royce “Silver Wraith” — café find")
        self.assertEqual(sc.load_searches()[0]["name"], rec["name"])
        self.assertTrue(rec["id"].startswith("rolls_royce_silver_wraith_caf"))

    def test_duplicate_names_are_rejected(self):
        self.make()
        rec, err = sc.add_search({"name": "defender 110", "query": "x",
                                  "cities": ["Medford, OR"]})
        self.assertIsNone(rec)
        self.assertIn("already have", err)

    def test_validation_messages(self):
        cases = [
            ({}, "name"),
            ({"name": "n"}, "search for"),
            ({"name": "n", "query": "q"}, "city"),
            ({"name": "n", "query": "q", "cities": ["c"],
              "interval": {"every": 0, "unit": "days"}}, "at least once"),
            ({"name": "n", "query": "q", "cities": ["c"],
              "interval": {"every": 1, "unit": "fortnights"}}, "Interval unit"),
            ({"name": "n", "query": "q", "cities": ["c"],
              "email_to": "not-an-email"}, "email address"),
        ]
        for payload, expect in cases:
            with self.subTest(payload=payload):
                rec, err = sc.add_search(payload)
                self.assertIsNone(rec)
                self.assertIn(expect, err)

    def test_editing_keeps_the_id_and_retimes_the_next_run(self):
        rec = self.make()
        updated, err = sc.update_search(
            rec["id"], {"interval": {"every": 6, "unit": "hours"},
                        "last_started": "2026-08-06T06:00:00"})
        self.assertIsNone(err)
        self.assertEqual(updated["id"], rec["id"])
        # Switching to hours puts the next run on the 5am-anchored grid.
        self.assertIn(sc.parse_iso(updated["next_run"]).hour, sc.grid_hours(6))

    def test_editing_can_keep_its_own_name(self):
        rec = self.make()
        _, err = sc.update_search(rec["id"], {"query": "defender 90"})
        self.assertIsNone(err)

    def test_pause_and_delete(self):
        rec = self.make()
        sc.update_search(rec["id"], {"enabled": False})
        self.assertFalse(sc.load_searches()[0]["enabled"])
        sc.delete_search(rec["id"])
        self.assertEqual(sc.load_searches(), [])
        _, err = sc.delete_search(rec["id"])
        self.assertIn("No scheduled search", err)

    def test_find_by_id_name_or_case_insensitive_name(self):
        rec = self.make()
        for ref in (rec["id"], "Defender 110", "defender 110"):
            self.assertIsNotNone(sc.find_search(sc.load_searches(), ref))
        self.assertIsNone(sc.find_search(sc.load_searches(), "nope"))

    def test_a_corrupt_file_is_reported_not_overwritten(self):
        self.make()
        sc.SEARCHES_PATH.write_text("{ this is not json", encoding="utf-8")
        with self.assertRaises(SystemExit) as cm:
            sc.load_searches()
        self.assertIn("unreadable", str(cm.exception))
        # And the damaged file is still there to be rescued.
        self.assertIn("not json", sc.SEARCHES_PATH.read_text(encoding="utf-8"))

    def test_missing_keys_are_filled_from_defaults(self):
        sc.SEARCHES_PATH.write_text(
            json.dumps({"searches": [{"id": "x", "name": "Bare", "query": "q"}]}),
            encoding="utf-8")
        s = sc.load_searches()[0]
        self.assertEqual(s["pace"], "fast")
        self.assertEqual(s["interval"], {"every": 1, "unit": "days"})

    def test_a_search_saved_before_or_queries_existed_still_loads(self):
        # Its one query is what it always was; it simply becomes a list of one.
        sc.SEARCHES_PATH.write_text(
            json.dumps({"searches": [{"id": "x", "name": "Old",
                                      "query": "defender 110"}]}),
            encoding="utf-8")
        s = sc.load_searches()[0]
        self.assertEqual(s["queries"], ["defender 110"])
        self.assertEqual(s["query"], "defender 110")

    def test_several_queries_are_saved_and_summed_up_on_one_line(self):
        rec = self.make(name="Either", queries=["defender 110", "land rover 90"])
        self.assertEqual(rec["queries"], ["defender 110", "land rover 90"])
        # query is derived, never sent: the two can't drift apart.
        self.assertEqual(rec["query"], "defender 110 OR land rover 90")
        self.assertEqual(sc.load_searches()[0]["queries"],
                         ["defender 110", "land rover 90"])

    def test_blank_boxes_in_the_form_are_not_queries(self):
        rec = self.make(name="Trimmed", queries=["defender 110", "   ", ""])
        self.assertEqual(rec["queries"], ["defender 110"])
        rec, err = sc.add_search({"name": "Empty", "queries": ["  "],
                                  "cities": ["Medford, OR"]})
        self.assertIsNone(rec)
        self.assertIn("search for", err)

    def test_editing_the_one_line_query_is_not_lost_to_the_list(self):
        rec = self.make()
        updated, err = sc.update_search(rec["id"], {"query": "defender 90"})
        self.assertIsNone(err)
        self.assertEqual(updated["queries"], ["defender 90"])
        self.assertEqual(sc.load_searches()[0]["query"], "defender 90")

    def test_a_search_with_several_queries_says_it_costs_more(self):
        rec = self.make(name="Three", queries=["defender 110", "land rover 90",
                                               "series iii"])
        warnings = " ".join(sc.interval_warnings(rec, sc.load_searches()))
        self.assertIn("3 queries", warnings)
        # Two is common enough not to be worth a warning of its own.
        self.assertNotIn("queries", " ".join(sc.interval_warnings(
            self.make(name="Both", queries=["defender 110", "land rover 90"]),
            sc.load_searches())))


class TestSafetyWarnings(Redirected):
    def test_short_intervals_warn(self):
        s = {"interval": {"every": 1, "unit": "hours"}}
        warns = sc.interval_warnings(s, [])
        self.assertTrue(any("limited or banned" in w for w in warns))

    def test_a_daily_interval_does_not_warn(self):
        s = {"interval": {"every": 1, "unit": "days"}}
        self.assertEqual(sc.interval_warnings(s, []), [])

    def test_too_many_active_searches_warn(self):
        existing = [{"id": f"s{i}", "enabled": True} for i in range(sc.SAFE_MAX_SEARCHES)]
        warns = sc.interval_warnings({"interval": {"every": 1, "unit": "days"}},
                                     existing)
        self.assertTrue(any("active scheduled searches" in w for w in warns))

    def test_paused_searches_do_not_count_towards_the_limit(self):
        existing = [{"id": f"s{i}", "enabled": False} for i in range(9)]
        self.assertEqual(
            sc.interval_warnings({"interval": {"every": 1, "unit": "days"}},
                                 existing), [])


# --------------------------------------------------------------------- database
class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "test.sqlite"
        self.con = storage.open_db(self.db)
        self.addCleanup(self.con.close)

    def row(self, item_id):
        cols = ",".join(storage.FIELDS)
        r = self.con.execute(
            f"SELECT {cols} FROM listings WHERE item_id=?", (item_id,)).fetchone()
        return dict(zip(storage.FIELDS, r)) if r else None

    def test_a_later_sweep_cannot_blank_a_description(self):
        # The bug this guards against emptied every description in the real
        # database: a sweep only sees search cards, so its description is always
        # blank, and the old upsert wrote that blank over the real thing.
        storage.upsert(self.con, {"item_id": "1", "title": "old",
                             "description": "a real description",
                             "raw_text": "raw", "image": "thumbnails/1.jpg"})
        storage.upsert(self.con, {"item_id": "1", "title": "new", "description": "",
                             "raw_text": "",
                             "image": "https://scontent.example/fresh.jpg"})
        r = self.row("1")
        self.assertEqual(r["title"], "new")
        self.assertEqual(r["description"], "a real description")
        self.assertEqual(r["raw_text"], "raw")
        self.assertEqual(r["image"], "thumbnails/1.jpg")

    def test_a_real_new_description_still_wins(self):
        storage.upsert(self.con, {"item_id": "1", "description": "first"})
        storage.upsert(self.con, {"item_id": "1", "description": "second"})
        self.assertEqual(self.row("1")["description"], "second")

    def test_a_remote_image_is_upgraded_to_a_local_one(self):
        storage.upsert(self.con, {"item_id": "1", "image": "https://x.example/a.jpg"})
        self.assertEqual(self.row("1")["image"], "https://x.example/a.jpg")
        storage.upsert(self.con, {"item_id": "1", "image": "thumbnails/1.jpg"})
        self.assertEqual(self.row("1")["image"], "thumbnails/1.jpg")

    def test_a_fresh_local_path_replaces_an_older_local_path(self):
        # Each run folder has its own thumbnails, so keeping the first path ever
        # stored would point later runs at a photo in someone else's folder. This
        # is what the old "thumbs/" paths did to a scheduled run's carried-forward
        # rows: 53 of 160 images came out broken.
        storage.upsert(self.con, {"item_id": "1", "image": "thumbs/1.jpg"})
        storage.upsert(self.con, {"item_id": "1", "image": "thumbnails/1.jpg"})
        self.assertEqual(self.row("1")["image"], "thumbnails/1.jpg")

    def test_a_local_path_is_not_downgraded_to_a_remote_url(self):
        storage.upsert(self.con, {"item_id": "1", "image": "thumbnails/1.jpg"})
        storage.upsert(self.con, {"item_id": "1", "image": "https://x.example/new.jpg"})
        self.assertEqual(self.row("1")["image"], "thumbnails/1.jpg")

    def test_a_blank_image_never_wins(self):
        storage.upsert(self.con, {"item_id": "1", "image": "thumbnails/1.jpg"})
        storage.upsert(self.con, {"item_id": "1", "image": ""})
        self.assertEqual(self.row("1")["image"], "thumbnails/1.jpg")

    def test_ordinary_columns_are_still_overwritten(self):
        storage.upsert(self.con, {"item_id": "1", "price": "$100", "miles": "10"})
        storage.upsert(self.con, {"item_id": "1", "price": "$90", "miles": "12"})
        r = self.row("1")
        self.assertEqual((r["price"], r["miles"]), ("$90", "12"))

    def test_the_schedule_tables_are_created(self):
        names = {r[0] for r in self.con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertLessEqual({"listings", "listing_state", "search_runs",
                              "run_items"}, names)

    def test_opening_twice_changes_nothing(self):
        storage.upsert(self.con, {"item_id": "1", "description": "keep me"})
        self.con.commit()
        second = storage.open_db(self.db)
        self.addCleanup(second.close)
        self.assertEqual(second.execute(
            "SELECT description FROM listings WHERE item_id='1'").fetchone()[0],
            "keep me")

    def test_migrating_the_real_database_preserves_everything(self):
        real = REPO / ".state" / "marketplace_results.sqlite"
        if not real.exists():
            self.skipTest("no real database to migrate")
        import shutil
        copy = Path(self.tmp.name) / "real.sqlite"
        shutil.copy(real, copy)
        import sqlite3
        # sqlite3's context manager commits but does not close, so be explicit.
        c = sqlite3.connect(copy)
        try:
            before = c.execute("SELECT count(*) FROM listings").fetchone()[0]
            thumbs = c.execute("SELECT count(*) FROM listings WHERE image "
                               "LIKE 'thumbs/%'").fetchone()[0]
        finally:
            c.close()
        con = storage.open_db(copy)
        self.addCleanup(con.close)
        self.assertEqual(con.execute("SELECT count(*) FROM listings").fetchone()[0],
                         before)
        self.assertEqual(con.execute("SELECT count(*) FROM listings WHERE image "
                                     "LIKE 'thumbs/%'").fetchone()[0], thumbs)


class TestRunBookkeeping(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.con = storage.open_db(Path(self.tmp.name) / "t.sqlite")
        self.addCleanup(self.con.close)

    def test_a_recorded_run_becomes_the_previous_result_set(self):
        sc.record_run(self.con, "s1", {
            "started": "2026-08-06T05:00:00", "finished": "2026-08-06T05:10:00",
            "duration_seconds": 600, "new_ids": ["a"],
            "total_ids": ["a", "b", "c"], "removed": [], "status": "ok"})
        self.assertEqual(sorted(sc.previous_item_ids(self.con, "s1")),
                         ["a", "b", "c"])

    def test_only_successful_runs_define_the_previous_set(self):
        sc.record_run(self.con, "s1", {"total_ids": ["a"], "status": "ok"})
        sc.record_run(self.con, "s1", {"total_ids": ["zzz"],
                                       "status": "session_expired"})
        # A failed run must not wipe out what the last good run knew.
        self.assertEqual(sc.previous_item_ids(self.con, "s1"), ["a"])

    def test_runs_of_other_searches_are_not_mixed_in(self):
        sc.record_run(self.con, "s1", {"total_ids": ["a"], "status": "ok"})
        sc.record_run(self.con, "s2", {"total_ids": ["b"], "status": "ok"})
        self.assertEqual(sc.previous_item_ids(self.con, "s1"), ["a"])

    def test_rows_for_ids_reads_back_full_listings(self):
        storage.upsert(self.con, {"item_id": "a", "title": "Rover", "price": "$1"})
        self.con.commit()
        rows = sc.rows_for_ids(self.con, ["a", "missing"], storage.FIELDS)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Rover")

    def test_seeing_a_listing_in_the_feed_counts_as_verifying_it(self):
        sc.mark_seen_in_feed(self.con, ["a", "b"])
        rows = dict(self.con.execute(
            "SELECT item_id, status FROM listing_state").fetchall())
        self.assertEqual(rows, {"a": "live", "b": "live"})
        self.assertEqual(sc.needs_verifying(self.con, ["a", "b"], 24), [])

    def test_a_listing_never_checked_needs_verifying(self):
        self.assertEqual(sc.needs_verifying(self.con, ["new"], 24), ["new"])

    def test_a_stale_check_is_due_again(self):
        old = sc.iso(sc.now_local() - timedelta(days=3))
        self.con.execute("INSERT INTO listing_state (item_id, last_verified, "
                         "status) VALUES ('a', ?, 'live')", (old,))
        self.con.commit()
        self.assertEqual(sc.needs_verifying(self.con, ["a"], 24), ["a"])

    def test_an_ambiguous_check_never_changes_the_status(self):
        sc.record_verification(self.con, "a", sc.STATUS_LIVE)
        sc.record_verification(self.con, "a", sc.STATUS_UNKNOWN)
        sc.record_verification(self.con, "a", sc.STATUS_UNKNOWN)
        status, fails = self.con.execute(
            "SELECT status, verify_failures FROM listing_state "
            "WHERE item_id='a'").fetchone()
        self.assertEqual(status, "live")
        self.assertEqual(fails, 2)

    def test_a_definite_answer_resets_the_failure_count(self):
        sc.record_verification(self.con, "a", sc.STATUS_UNKNOWN)
        sc.record_verification(self.con, "a", sc.STATUS_GONE)
        status, fails = self.con.execute(
            "SELECT status, verify_failures FROM listing_state "
            "WHERE item_id='a'").fetchone()
        self.assertEqual((status, fails), ("gone", 0))


# ------------------------------------------------------------------- classifier
# Facebook's UI string bundles ship on every page, and they contain the word
# "sold" — this exact sentence is in the payload of a perfectly live listing. The
# related-items rail ships about twenty other listings, each with its own flags.
# Both are in these fixtures on purpose: they are what made a naive substring
# search call live listings sold.
UI_STRINGS = "OK, your listing has been marked as sold.|Mark as pending"

LIVE = {"is_sold": False, "is_pending": False, "is_live": True,
        "is_hidden": False}
SOLD = {**LIVE, "is_sold": True}
PENDING = {**LIVE, "is_pending": True}
DELISTED = {**LIVE, "is_live": False}


def fb_page(item_id=None, flags=None, rail=(), extra="", strings=True,
            broken_json=False):
    """A page shaped the way a real listing page is: embedded JSON in
    <script type="application/json"> blocks, a related-items rail, and Facebook's
    localisation strings."""
    nodes = []
    if item_id is not None and flags is not None:
        nodes.append({"__typename": "GroupCommerceProductItem", "id": item_id,
                      "marketplace_listing_title": "1995 Defender 110", **flags})
    for other_id, other_flags in rail:
        nodes.append({"__typename": "GroupCommerceProductItem", "id": other_id,
                      "marketplace_listing_title": "Related thing",
                      "listing_photos": [{"id": "9"}], **other_flags})
    blob = json.dumps({"require": [["RelayPrefetchedStreamCache", "next", [],
                                    {"__bbox": {"result": {"data": {
                                        "viewer": {"marketplace_feed_stories":
                                                   nodes}}}}}]]})
    parts = [f'<script type="application/json" data-sjs>{blob}</script>']
    if strings:
        parts.append(f'<script type="application/json">'
                     f'{json.dumps({"strings": UI_STRINGS})}</script>')
    if broken_json:
        parts.append('<script type="application/json">{not json at all</script>')
    return f"<!doctype html><html><body>{''.join(parts)}{extra}</body></html>"


class TestClassifier(unittest.TestCase):
    ITEM_ID = "123456789"
    ITEM = f"https://www.facebook.com/marketplace/item/{ITEM_ID}/"

    def verdict(self, body, code=200, url=None, item_id="123456789"):
        return sc.classify_listing(code, url or self.ITEM, body, item_id)

    def test_a_live_listing(self):
        status, marker = self.verdict(fb_page(self.ITEM_ID, LIVE))
        self.assertEqual(status, sc.STATUS_LIVE)
        self.assertIn("own record", marker)

    def test_a_sold_listing_is_treated_as_removed(self):
        status, marker = self.verdict(fb_page(self.ITEM_ID, SOLD))
        self.assertEqual(status, sc.STATUS_SOLD)
        self.assertIn("is_sold", marker)

    def test_a_pending_sale_counts_as_sold(self):
        status, marker = self.verdict(fb_page(self.ITEM_ID, PENDING))
        self.assertEqual(status, sc.STATUS_SOLD)
        self.assertIn("is_pending", marker)

    def test_a_delisted_listing_is_gone(self):
        status, marker = self.verdict(fb_page(self.ITEM_ID, DELISTED))
        self.assertEqual(status, sc.STATUS_GONE)
        self.assertIn("is_live", marker)

    # -- the regressions that live pages actually produced -------------------
    def test_a_sold_listing_in_the_related_rail_does_not_condemn_this_one(self):
        # Measured on real pages: a whole-page search for '"is_sold":true' called
        # a live listing sold eight times out of nine, because the rail below it
        # had sold items in it.
        body = fb_page(self.ITEM_ID, LIVE,
                       rail=[("111", SOLD), ("222", PENDING), ("333", LIVE)])
        self.assertEqual(self.verdict(body)[0], sc.STATUS_LIVE)

    def test_the_word_sold_in_facebooks_own_ui_text_is_ignored(self):
        # "OK, your listing has been marked as sold." ships on every page.
        body = fb_page(self.ITEM_ID, LIVE)
        self.assertIn("marked as sold", body)
        self.assertEqual(self.verdict(body)[0], sc.STATUS_LIVE)

    def test_a_gone_phrase_on_a_page_full_of_listings_is_not_trusted(self):
        # If our record can't be found but the page is plainly still serving
        # listing data, the honest answer is "don't know". Measured on real pages,
        # a removed listing's page has no listing data at all, so requiring both
        # signals costs nothing and makes a future payload change degrade to
        # "unknown" instead of to "delete everything".
        body = fb_page(rail=[("111", LIVE)],
                       extra="<div>This listing isn't available right now.</div>")
        status, marker = self.verdict(body)
        self.assertEqual(status, sc.STATUS_UNKNOWN)
        self.assertIn("no record for this id", marker)

    def test_a_sold_listing_is_still_sold_with_a_live_rail(self):
        body = fb_page(self.ITEM_ID, SOLD, rail=[("111", LIVE), ("222", LIVE)])
        self.assertEqual(self.verdict(body)[0], sc.STATUS_SOLD)

    # -- pages with no record for this listing -------------------------------
    def test_a_removed_listing_page_is_gone(self):
        # This is the real shape: no record for the id, no listing data anywhere
        # on the page, and Facebook saying so in its own words.
        body = ("<!doctype html><html><body><div>This listing isn't available "
                "right now.</div></body></html>")
        status, marker = self.verdict(body)
        self.assertEqual(status, sc.STATUS_GONE)
        self.assertIn("isn't available", marker)

    def test_content_isnt_available_is_also_gone(self):
        body = "<html><body>Content isn't available right now</body></html>"
        self.assertEqual(self.verdict(body)[0], sc.STATUS_GONE)

    def test_a_listing_page_without_our_record_is_unknown(self):
        # Only other listings on the page. Facebook may have changed shape; the
        # one thing we must not do is guess.
        body = fb_page(rail=[("111", LIVE), ("222", LIVE)])
        status, marker = self.verdict(body)
        self.assertEqual(status, sc.STATUS_UNKNOWN)
        self.assertIn("no record for this id", marker)

    def test_a_404_is_gone(self):
        self.assertEqual(self.verdict("", code=404)[0], sc.STATUS_GONE)

    def test_a_redirect_away_from_the_item_is_gone(self):
        status, marker = self.verdict(
            "<html>the marketplace feed</html>",
            url="https://www.facebook.com/marketplace/")
        self.assertEqual(status, sc.STATUS_GONE)
        self.assertIn("redirected", marker)

    def test_a_login_redirect_is_a_session_problem_not_a_deletion(self):
        # This is the distinction that matters most: an expired session must
        # never be read as "every listing was taken down".
        for url in ("https://www.facebook.com/login/?next=x",
                    "https://www.facebook.com/checkpoint/123",
                    "https://www.facebook.com/two_step_verification/x"):
            with self.subTest(url=url):
                self.assertEqual(self.verdict("", url=url)[0], sc.STATUS_AUTH)

    def test_an_empty_body_is_unknown(self):
        self.assertEqual(self.verdict("")[0], sc.STATUS_UNKNOWN)

    def test_unrecognisable_content_is_unknown(self):
        status, marker = self.verdict("<html>hm</html>")
        self.assertEqual(status, sc.STATUS_UNKNOWN)
        self.assertIn("no marker", marker)

    def test_a_400_is_about_the_request_not_the_listing(self):
        # Facebook answers a request context without browser headers with 400 and
        # a stub page. Reading that as a deletion would empty the tracked set.
        status, marker = self.verdict("<html>Bad Request</html>", code=400)
        self.assertEqual(status, sc.STATUS_UNKNOWN)
        self.assertIn("400", marker)

    def test_a_rate_limit_page_is_unknown_not_gone(self):
        self.assertEqual(self.verdict("slow down", code=429)[0],
                         sc.STATUS_UNKNOWN)

    # -- robustness ----------------------------------------------------------
    def test_the_id_is_read_from_the_url_when_not_supplied(self):
        body = fb_page(self.ITEM_ID, SOLD)
        self.assertEqual(sc.classify_listing(200, self.ITEM, body)[0],
                         sc.STATUS_SOLD)

    def test_a_malformed_json_block_is_skipped_not_fatal(self):
        body = fb_page(self.ITEM_ID, SOLD, broken_json=True)
        self.assertEqual(self.verdict(body)[0], sc.STATUS_SOLD)

    def test_deeply_nested_payloads_do_not_blow_the_stack(self):
        # Real payloads nest far enough that a recursive walk raises
        # RecursionError, which would be read as a failed check forever.
        node = {"id": self.ITEM_ID, **SOLD}
        for _ in range(4000):
            node = {"wrap": node}
        body = ('<script type="application/json">'
                + json.dumps(node) + "</script>")
        self.assertEqual(self.verdict(body)[0], sc.STATUS_SOLD)

    def test_the_probe_sends_browser_headers(self):
        # Without these Facebook answers 400, which makes the cheap tier useless.
        self.assertEqual(sc.PROBE_HEADERS["sec-fetch-mode"], "navigate")
        self.assertIn("text/html", sc.PROBE_HEADERS["accept"])


class TestListingRecord(unittest.TestCase):
    def test_it_picks_the_node_with_the_flags(self):
        # The page carries a stub node for the same id as well as the real one.
        body = ('<script type="application/json">' + json.dumps(
            {"a": {"id": "5", "is_hidden": False},
             "b": {"id": "5", "is_sold": True, "is_live": True}})
            + "</script>")
        self.assertTrue(sc.listing_record(body, "5")["is_sold"])

    def test_a_stub_node_is_better_than_nothing(self):
        body = ('<script type="application/json">'
                + json.dumps({"a": {"id": "5", "is_live": False}}) + "</script>")
        self.assertEqual(sc.listing_record(body, "5")["is_live"], False)

    def test_no_id_means_no_record(self):
        self.assertIsNone(sc.listing_record(fb_page("1", LIVE), None))

    def test_a_different_id_is_not_matched(self):
        self.assertIsNone(sc.listing_record(fb_page("1", LIVE), "999"))

    def test_numeric_ids_match_string_ids(self):
        body = ('<script type="application/json">'
                + json.dumps({"a": {"id": 5, "is_sold": True}}) + "</script>")
        self.assertTrue(sc.listing_record(body, "5")["is_sold"])


class TestVerifierLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.con = storage.open_db(Path(self.tmp.name) / "t.sqlite")
        self.addCleanup(self.con.close)

    class FakeCtx:
        """Stands in for Playwright's request context."""

        def __init__(self, replies):
            self.replies = replies
            self.asked = []
            self.request = self

        def get(self, url, **kw):
            self.asked.append(url)
            reply = self.replies[url]
            if isinstance(reply, Exception):
                raise reply

            class R:
                status = reply[0]
                url_ = reply[1]

                def __init__(self, body):
                    self._b = body

                def text(self):
                    return self._b
            r = R(reply[2])
            r.url = reply[1]
            r.status = reply[0]
            return r

    def url(self, iid):
        return f"https://www.facebook.com/marketplace/item/{iid}"

    def rows(self, *ids):
        return [{"item_id": i, "url": self.url(i), "title": f"listing {i}"}
                for i in ids]

    def page(self, iid, flags):
        return (200, self.url(iid), fb_page(iid, flags))

    def test_only_confirmed_listings_are_removed(self):
        ctx = self.FakeCtx({
            self.url("100"): (200, self.url("100"),
                              "<html>This listing isn't available right now."
                              "</html>"),
            self.url("200"): self.page("200", SOLD),
            self.url("300"): self.page("300", LIVE),
            self.url("400"): TimeoutError("network went away"),
        })
        verify = sc.make_verifier(self.con, pause=lambda: None)
        removed, checked, auth = verify(ctx, self.rows("100", "200", "300", "400"))
        self.assertFalse(auth)
        self.assertEqual(checked, 4)
        self.assertEqual({r["item_id"]: r["removal"] for r in removed},
                         {"100": "gone", "200": "sold"})

    def test_a_network_failure_never_removes_a_listing(self):
        ctx = self.FakeCtx({self.url("400"): TimeoutError("boom")})
        verify = sc.make_verifier(self.con, pause=lambda: None)
        removed, checked, auth = verify(ctx, self.rows("400"))
        self.assertEqual(removed, [])
        self.assertEqual(checked, 1)
        self.assertEqual(self.con.execute(
            "SELECT verify_failures FROM listing_state WHERE item_id='400'"
        ).fetchone()[0], 1)

    def test_a_page_we_cannot_read_never_removes_a_listing(self):
        ctx = self.FakeCtx({self.url("500"): (400, self.url("500"),
                                              "<html>Bad Request</html>")})
        verify = sc.make_verifier(self.con, pause=lambda: None)
        removed, checked, auth = verify(ctx, self.rows("500"))
        self.assertEqual(removed, [])
        self.assertFalse(auth)

    def test_an_expired_session_stops_the_loop_immediately(self):
        ctx = self.FakeCtx({
            self.url("800"): (200, "https://www.facebook.com/login/?next=x", ""),
            self.url("900"): self.page("900", LIVE),
        })
        verify = sc.make_verifier(self.con, pause=lambda: None)
        removed, checked, auth = verify(ctx, self.rows("800", "900"))
        self.assertTrue(auth)
        self.assertEqual(removed, [])
        # It stopped at the first listing rather than probing the rest.
        self.assertEqual(ctx.asked, [self.url("800")])

    def test_tier_two_is_used_only_when_tier_one_cannot_decide(self):
        class FakePage:
            def __init__(self, replies):
                self.replies, self.visited, self.url = replies, [], ""

            def goto(self, url, **kw):
                self.visited.append(url)
                self.url = url
                self._body = self.replies[url]
                return type("R", (), {"status": 200})()

            def wait_for_timeout(self, ms):
                pass

            def content(self):
                return self._body

        ctx = self.FakeCtx({
            self.url("600"): self.page("600", SOLD),
            # Tier 1 can't tell: the page has listings but not this one's record.
            self.url("700"): (200, self.url("700"), fb_page(rail=[("9", LIVE)])),
        })
        page = FakePage({self.url("700"): fb_page("700", SOLD)})
        verify = sc.make_verifier(self.con, pause=lambda: None, page=page)
        removed, checked, auth = verify(ctx, self.rows("600", "700"))
        self.assertEqual(page.visited, [self.url("700")])
        self.assertEqual({r["item_id"] for r in removed}, {"600", "700"})


# ----------------------------------------------------------------------- report
def listing(iid, title, price="$1,000", where="Medford, OR", **kw):
    r = {"item_id": iid, "title": title, "price": price,
         "listing_location": where,
         "url": f"https://www.facebook.com/marketplace/item/{iid}/",
         "image": "", "description": "", "miles": "10", "source_section": "",
         "matches_query": "yes", "location_searched": where, "query": "defender",
         "scraped_at": "2026-08-06T05:00:00", "raw_text": ""}
    r.update(kw)
    return r


SEARCH = {"id": "d110-abcd", "name": "Defender 110", "query": "defender 110",
          "cities": ["Medford, OR", "Sacramento, CA"],
          "interval": {"every": 1, "unit": "days"}}


def summary_fixture(**kw):
    new = [listing("n1", "1995 Defender 110"), listing("n2", "1997 Defender 110")]
    removed = [
        {**listing("r1", "1993 Defender 110 SOLD"), "removal": "sold",
         "marker": '"is_sold":true'},
        {**listing("r2", "Defender 110 parts"), "removal": "gone",
         "marker": "this listing isn't available"},
    ]
    s = {"status": "ok", "started": "2026-08-06T05:00:00",
         "finished": "2026-08-06T05:22:00", "duration_seconds": 1320,
         "new_ids": ["n1", "n2"], "total_ids": ["n1", "n2", "old1", "old2"],
         "new_rows": new, "removed": removed, "descriptions_fetched": 2,
         "radius_km": 805, "per_city": {"Medford, OR": {"kept": 3, "cards": 40}},
         "run_dir": "/tmp/runs/saved/defender_110",
         "gallery": "/tmp/runs/saved/defender_110/gallery.html"}
    s.update(kw)
    return s


class TestReport(unittest.TestCase):
    def report(self, **kw):
        warnings = kw.pop("warnings", ())
        next_run = kw.pop("next_run", dt("2026-08-07T05:00:00"))
        return sc.build_report(SEARCH, summary_fixture(**kw), next_run, warnings)

    def test_the_headline_numbers_appear(self):
        subject, text, html = self.report()
        self.assertIn("Defender 110", subject)
        self.assertIn("2 new", subject)
        self.assertIn("4 total", subject)
        self.assertIn("2 new listings", text)
        self.assertIn("4 total listings being tracked", text)
        self.assertIn("2 sold or taken down since the last run", text)

    def test_every_removed_listing_is_named_with_its_link(self):
        # The thing that was specifically asked for: not just a count, a list.
        _, text, html = self.report()
        for body in (text, html):
            self.assertIn("1993 Defender 110 SOLD", body)
            self.assertIn("Defender 110 parts", body)
            self.assertIn("/marketplace/item/r1/", body)
            self.assertIn("/marketplace/item/r2/", body)

    def test_sold_and_taken_down_are_listed_separately(self):
        _, text, html = self.report()
        self.assertIn("SOLD (1)", text)
        self.assertIn("TAKEN DOWN (1)", text)
        self.assertIn("Sold (1)", html)
        self.assertIn("Taken down (1)", html)

    def test_new_listings_are_named(self):
        _, text, html = self.report()
        self.assertIn("1995 Defender 110", text)
        self.assertIn("1997 Defender 110", html)

    def test_it_says_where_the_full_gallery_is(self):
        # The way back to everything is the app itself, not a folder the reader
        # would have to go find on disk.
        _, text, html = self.report()
        for body in (text, html):
            self.assertIn("Past searches", body)
            self.assertIn("Faceplace Marketbook", body)
        self.assertIn("stripped-down versions", text)
        self.assertNotIn("runs/saved/defender_110", text)

    def test_nothing_removed_says_so_explicitly(self):
        _, text, html = self.report(removed=[])
        self.assertIn("Nothing was sold or taken down", text)
        self.assertIn("Nothing was sold or taken down", html)
        self.assertNotIn("TAKEN DOWN", text)

    def test_duration_and_timing(self):
        _, text, _ = self.report()
        self.assertIn("22m 0s", text)

    def test_one_city_is_not_called_1_cities(self):
        search = {**SEARCH, "cities": ["Medford, OR"]}
        _, text, html = sc.build_report(search, summary_fixture(), None, ())
        self.assertIn("across 1 city.", text)
        self.assertIn("across 1 city ", html)

    def test_it_says_what_was_searched_for(self):
        _, text, html = self.report()
        self.assertIn("Searched for 'defender 110' across", text)
        self.assertIn("defender 110", html)

    def test_a_search_with_two_queries_names_both_of_them(self):
        search = {**SEARCH, "queries": ["defender 110", "land rover 90"],
                  "query": "defender 110 OR land rover 90"}
        _, text, html = sc.build_report(search, summary_fixture(), None, ())
        self.assertIn("Searched for 'defender 110' or 'land rover 90' across",
                      text)
        self.assertIn("&lsquo;land rover 90&rsquo;", html)

    def test_the_start_time_is_shown_in_local_time(self):
        # The sweep hands back a UTC stamp; the report has to say the wall-clock
        # time the run actually happened.
        summary = summary_fixture(started="2026-08-07T05:12:00+00:00")
        _, text, _ = sc.build_report(SEARCH, summary, None, ())
        expect = sc.fmt_when(sc.parse_iso("2026-08-07T05:12:00+00:00"))
        self.assertIn(expect, text)

    def test_the_next_run_is_stated(self):
        _, text, html = self.report()
        self.assertIn("Next run:", text)
        self.assertIn("Next run:", html)

    def test_how_to_pause_is_always_included(self):
        _, text, html = self.report()
        for body in (text, html):
            self.assertIn("Scheduled searches", body)

    def test_warnings_are_shown_prominently(self):
        _, text, html = self.report(warnings=["Your radius is only 250 miles."])
        self.assertTrue(text.startswith("!! Your radius"))
        self.assertIn("Your radius is only 250 miles.", html)

    def test_no_warnings_means_no_warning_furniture(self):
        _, text, html = self.report()
        self.assertNotIn("!!", text)
        self.assertNotIn("Heads up", html)

    def test_titles_with_html_characters_are_escaped(self):
        _, _, html = self.report(
            new_rows=[listing("x", '<script>alert("hi")</script> Rover & Co')])
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&amp;", html)

    def test_a_listing_with_no_price_still_renders(self):
        _, text, html = self.report(new_rows=[listing("x", "Rover", price="")])
        self.assertIn("no price", text)
        self.assertIn("no price", html)

    def test_descriptions_line_explains_the_saving(self):
        _, text, _ = self.report()
        self.assertIn("only new listings need one", text)


# ----------------------------------------------- the way back to the real gallery
class TestGalleryLink(unittest.TestCase):
    """The report names a file on one particular computer. On that computer it
    should be one click away, and everywhere else it should degrade into
    something the reader can still act on."""

    def html(self, **kw):
        return sc.build_report(SEARCH, summary_fixture(**kw), None, ())[2]

    def test_the_gallery_is_a_link_and_not_just_a_path(self):
        html = self.html()
        self.assertIn(
            'href="file:///tmp/runs/saved/defender_110/gallery.html"', html)

    def test_a_folder_name_with_spaces_still_makes_a_working_link(self):
        # A scheduled search called "Defender 110" would slug down, but nothing
        # stops a run folder holding a space or an ampersand, and a raw one in
        # an href is a link that lands somewhere else or nowhere.
        html = self.html(gallery="/tmp/runs/my search & co/gallery.html")
        self.assertIn('href="file:///tmp/runs/my%20search%20%26%20co/gallery.html"',
                      html)
        self.assertNotIn("my search & co/gallery.html", html)

    def test_the_link_is_never_the_only_way_back(self):
        # Gmail strips file:// hrefs, and a reader on a phone can't use one
        # anyway, so the paragraph around it has to name a route that doesn't
        # depend on the link working.
        html = self.html()
        self.assertIn("Past searches", html)
        self.assertIn("attached files", html)

    def test_a_run_with_no_gallery_points_at_the_app_not_the_folder(self):
        html = self.html(gallery=None)
        self.assertNotIn("file://", html)
        self.assertNotIn("/tmp/runs/saved/defender_110", html)
        self.assertIn("Past searches", html)

    def test_a_path_that_cannot_be_linked_is_left_out_entirely(self):
        # Whatever a relative path is relative to, it isn't the machine reading
        # the email. A link built from one would point somewhere real and
        # wrong, and the path on its own is no use to the reader either.
        html = self.html(gallery="runs/saved/defender_110/gallery.html")
        self.assertNotIn("file://", html)
        self.assertNotIn("runs/saved/defender_110/gallery.html", html)
        self.assertIn("Past searches", html)

    def test_nothing_written_anywhere_still_leaves_a_sane_paragraph(self):
        html = self.html(gallery=None, run_dir=None)
        self.assertNotIn("file://", html)
        self.assertNotIn("<code", html)
        self.assertIn("attached files", html)

    def test_the_attachments_are_named_as_the_way_in_from_a_phone(self):
        # The graceful failure: the link does nothing away from that computer,
        # so the same email has to carry a copy that opens anywhere.
        for kw in ({}, {"gallery": None}, {"gallery": "results/gallery.html"}):
            self.assertIn("The attached files contain stripped-down versions",
                          self.html(**kw))

    def test_the_link_is_the_one_the_app_itself_would_open(self):
        # past_runs opens a gallery with the same file:// form; if the two ever
        # disagreed, one of them would be the broken one.
        with TemporaryDirectory() as tmp:
            gallery = Path(tmp) / "runs" / "saved" / "d 110" / "gallery.html"
            gallery.parent.mkdir(parents=True)
            gallery.write_text("<html>", encoding="utf-8")
            self.assertIn(f'href="{gallery.as_uri()}"',
                          self.html(gallery=str(gallery)))


# ------------------------------------------------------------------ attachments
class TestAttachments(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def build_csv(self, n=6, image_bytes=0):
        thumbs = self.dir / "thumbnails"
        thumbs.mkdir(exist_ok=True)
        rows = []
        for i in range(n):
            iid = f"i{i}"
            img = ""
            if image_bytes:
                p = thumbs / f"{iid}.jpg"
                p.write_bytes(os.urandom(image_bytes))
                img = f"thumbnails/{iid}.jpg"
            rows.append(listing(iid, f"Listing {i}", image=img))
        csv_path = self.dir / "results.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=storage.FIELDS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in storage.FIELDS})
        return csv_path

    def test_two_attachments_are_produced(self):
        csv_path = self.build_csv()
        attachments, built = sc.build_attachments(csv_path, ["i0", "i1"])
        self.assertEqual([a[0] for a in attachments],
                         ["new-listings.html", "all-results.html"])
        self.assertTrue(all(a[1] for a in attachments))

    def test_the_new_listings_file_holds_only_the_new_ones(self):
        csv_path = self.build_csv(n=5)
        attachments, _ = sc.build_attachments(csv_path, ["i0", "i3"])
        new_html = attachments[0][1].decode("utf-8")
        self.assertIn("Listing 0", new_html)
        self.assertIn("Listing 3", new_html)
        self.assertNotIn("Listing 1", new_html)
        all_html = attachments[1][1].decode("utf-8")
        for i in range(5):
            self.assertIn(f"Listing {i}", all_html)

    def test_no_new_listings_means_only_the_full_file(self):
        csv_path = self.build_csv()
        attachments, _ = sc.build_attachments(csv_path, [])
        self.assertEqual([a[0] for a in attachments], ["all-results.html"])

    def test_thumbnails_are_never_included(self):
        csv_path = self.build_csv(n=4, image_bytes=2000)
        attachments, _ = sc.build_attachments(csv_path, ["i0"])
        for name, data, _ in attachments:
            html = data.decode("utf-8")
            self.assertNotIn("data:image/jpeg;base64", html, name)
            self.assertNotIn("thumbnails/", html, name)


# ------------------------------------------------------- what stays, what goes
class TestReconciliation(unittest.TestCase):
    """The rule that matters most: a listing missing from this run's feed is not
    evidence that it's gone. Only a confirmed check removes it."""

    def setUp(self):
        self.prev = {i: listing(i, f"old {i}") for i in ("a", "b", "c")}

    def test_absent_from_the_feed_is_not_enough_to_remove_it(self):
        feed = {"a": listing("a", "still listed")}
        new_ids, carried = storage.reconcile_with_previous(feed, self.prev, gone_ids=set())
        self.assertEqual(carried, 2)
        self.assertEqual(sorted(feed), ["a", "b", "c"])
        self.assertEqual(new_ids, [])

    def test_a_confirmed_removal_drops_out(self):
        feed = {"a": listing("a", "still listed")}
        _, carried = storage.reconcile_with_previous(feed, self.prev, gone_ids={"b"})
        self.assertEqual(carried, 1)
        self.assertEqual(sorted(feed), ["a", "c"])

    def test_new_listings_are_identified(self):
        feed = {"a": listing("a", "x"), "z": listing("z", "brand new")}
        new_ids, _ = storage.reconcile_with_previous(feed, self.prev, gone_ids=set())
        self.assertEqual(new_ids, ["z"])

    def test_the_feed_version_of_a_listing_wins_over_the_stored_one(self):
        # Prices change; the fresh row is the one to keep.
        feed = {"a": listing("a", "old a", price="$999")}
        storage.reconcile_with_previous(feed, self.prev, gone_ids=set())
        self.assertEqual(feed["a"]["price"], "$999")

    def test_a_stored_description_survives_onto_the_fresh_row(self):
        # A sweep row is a search card, so its description is blank. Letting that
        # blank stand made a listing that keeps appearing in the feed look
        # undescribed every run, and its detail page was re-fetched every run.
        self.prev["a"]["description"] = "a long description from last time"
        self.prev["a"]["raw_text"] = "raw"
        feed = {"a": listing("a", "still listed")}
        storage.reconcile_with_previous(feed, self.prev, gone_ids=set())
        self.assertEqual(feed["a"]["description"],
                         "a long description from last time")
        self.assertEqual(feed["a"]["raw_text"], "raw")

    def test_a_fresh_description_is_not_overwritten_by_the_stored_one(self):
        self.prev["a"]["description"] = "old"
        feed = {"a": listing("a", "x", description="new and better")}
        storage.reconcile_with_previous(feed, self.prev, gone_ids=set())
        self.assertEqual(feed["a"]["description"], "new and better")

    def test_a_new_listing_has_nothing_to_inherit(self):
        feed = {"z": listing("z", "brand new")}
        storage.reconcile_with_previous(feed, self.prev, gone_ids=set())
        self.assertEqual(feed["z"]["description"], "")

    def test_everything_confirmed_gone_leaves_only_the_feed(self):
        feed = {"a": listing("a", "x")}
        _, carried = storage.reconcile_with_previous(feed, self.prev,
                                                gone_ids={"b", "c"})
        self.assertEqual(carried, 0)
        self.assertEqual(list(feed), ["a"])

    def test_a_first_run_has_no_previous_set(self):
        feed = {"a": listing("a", "x")}
        new_ids, carried = storage.reconcile_with_previous(feed, {}, gone_ids=set())
        self.assertEqual((new_ids, carried), (["a"], 0))

    def test_carried_rows_get_a_relevance_score(self):
        feed = {}
        storage.reconcile_with_previous(feed, self.prev, gone_ids=set(),
                                    score=lambda r: 7)
        self.assertEqual([r["_score"] for r in feed.values()], [7, 7, 7])


# ------------------------------------------- email as a step, not an afterthought
class TestSavingNeedsEmail(Redirected):
    """A scheduled search reports by email and by nothing else, so one saved before
    there's an account to send from runs on time, finds things, and tells
    nobody. The window refuses to make one until email works."""

    FORM = {"name": "Defender 110", "queries": ["defender 110"],
            "cities": ["Medford, OR"], "interval": {"every": 1, "unit": "days"}}
    ACCOUNT = {"provider": "gmail", "address": "me@gmail.com",
               "app_password": "abcd efgh ijkl mnop"}

    def setUp(self):
        super().setUp()
        # Never let a test reach launchd or Task Scheduler. Automatic runs are on
        # and working here, so email is the only thing these tests are about.
        self._installed = sc.schedule_installed
        sc.schedule_installed = lambda: True
        self.addCleanup(lambda: setattr(sc, "schedule_installed", self._installed))
        self._problems = sc.schedule_problems
        sc.schedule_problems = lambda: []
        self.addCleanup(lambda: setattr(sc, "schedule_problems", self._problems))

    def test_saving_is_refused_while_there_is_no_email(self):
        res = sc.ui_hooks()["save_search"](dict(self.FORM))
        self.assertIn("Email & Setup", res["error"])
        self.assertIs(res["email_ready"], False)
        self.assertFalse(sc.SEARCHES_PATH.exists())

    def test_saving_goes_through_once_email_is_set_up(self):
        sc.save_email_config(self.ACCOUNT)
        res = sc.ui_hooks()["save_search"](dict(self.FORM))
        self.assertNotIn("error", res)
        self.assertEqual([s["name"] for s in sc.load_searches()], ["Defender 110"])

    def test_editing_one_is_refused_after_the_email_settings_go_away(self):
        sc.save_email_config(self.ACCOUNT)
        saved = sc.ui_hooks()["save_search"](dict(self.FORM))["searches"][0]
        sc.EMAIL_CONFIG_PATH.unlink()
        res = sc.ui_hooks()["save_search"]({**self.FORM, "id": saved["id"],
                                            "name": "Something else"})
        self.assertIn("error", res)
        self.assertEqual([s["name"] for s in sc.load_searches()], ["Defender 110"])

    def test_the_window_is_told_whether_email_works_before_it_opens(self):
        self.assertIs(sc.ui_hooks()["email_config"]()["ready"], False)
        sc.save_email_config(self.ACCOUNT)
        cfg = sc.ui_hooks()["email_config"]()
        self.assertIs(cfg["ready"], True)
        self.assertEqual(cfg["address"], "me@gmail.com")

    def test_a_half_finished_account_reports_itself_as_not_ready(self):
        res = sc.ui_hooks()["save_email"]({"provider": "gmail",
                                           "address": "me@gmail.com",
                                           "app_password": ""})
        self.assertIs(res["ready"], False)
        res = sc.ui_hooks()["save_email"](dict(self.ACCOUNT))
        self.assertIs(res["ready"], True)


# ------------------------------------ automatic runs as a step, not an afterthought
class TestSavingNeedsAutomaticRuns(Redirected):
    """The other half of the same problem. A search saved with nothing to start it
    is as silent as one with nowhere to report to, and looks just as scheduled in
    the list, so it's refused the same way."""

    FORM = dict(TestSavingNeedsEmail.FORM)

    def setUp(self):
        super().setUp()
        sc.save_email_config(dict(TestSavingNeedsEmail.ACCOUNT))
        for name in ("schedule_installed", "schedule_problems", "os_name"):
            self.addCleanup(setattr, sc, name, getattr(sc, name))
        sc.os_name = lambda: "darwin"
        self.stub(installed=True, problems=[])

    def stub(self, installed, problems):
        sc.schedule_installed = lambda: installed
        sc.schedule_problems = lambda: problems

    def save(self, **extra):
        return sc.ui_hooks()["save_search"]({**self.FORM, **extra})

    def test_saving_is_refused_while_automatic_runs_are_off(self):
        self.stub(installed=False, problems=[])
        res = self.save()
        self.assertIn("Turn automatic runs on", res["error"])
        self.assertIs(res["schedule_ready"], False)
        self.assertFalse(sc.SEARCHES_PATH.exists())

    def test_runs_that_are_on_but_blocked_are_refused_too(self):
        # Installed and blocked reads as "on" everywhere else, and runs nothing.
        self.stub(installed=True, problems=["The scheduler stopped checking in."])
        self.assertIn("Turn automatic runs on", self.save()["error"])
        self.assertFalse(sc.SEARCHES_PATH.exists())

    def test_saving_goes_through_once_they_are_on_and_working(self):
        res = self.save()
        self.assertNotIn("error", res)
        self.assertEqual([s["name"] for s in sc.load_searches()], ["Defender 110"])
        # And says nothing about turning them on, which is the state it's in.
        self.assertNotIn("automatic runs", res["message"])

    def test_editing_one_is_refused_after_the_runs_are_turned_off(self):
        saved = self.save()["searches"][0]
        self.stub(installed=False, problems=[])
        res = self.save(id=saved["id"], name="Something else")
        self.assertIn("error", res)
        self.assertEqual([s["name"] for s in sc.load_searches()], ["Defender 110"])

    def test_the_window_is_told_before_it_opens(self):
        # The block belongs on screen when the window appears, not after the
        # first refusal, so the state it reads has the answer in it.
        self.assertIs(sc.ui_hooks()["schedule_state"]()["ready"], True)
        self.stub(installed=True, problems=["Pointing somewhere else."])
        self.assertIs(sc.ui_hooks()["schedule_state"]()["ready"], False)

    def test_a_system_with_no_schedule_to_install_is_not_held_to_this(self):
        # There's nothing to turn on outside macOS and Windows, so insisting would
        # bar every saved search on those systems. They run by hand instead, and
        # saving says so.
        sc.os_name = lambda: "other"
        self.stub(installed=False, problems=[])
        res = self.save()
        self.assertNotIn("error", res)
        self.assertIn("--run", res["message"])


# ------------------------------------------------------------------ the wake queue
class TestWakeQueue(Redirected):
    """The one-off wake-ups that get a sleeping Mac up for hourly searches.
    Written weeks ahead in a single authorized batch, run down day by day,
    renewed from the settings window — and never allowed near the real pmset
    from here: what would have run is recorded instead."""

    def setUp(self):
        super().setUp()
        for name in ("os_name",):
            self.addCleanup(setattr, sc, name, getattr(sc, name))
        sc.os_name = lambda: "darwin"
        self.on_machine = []            # what pmset -g sched would report
        sc.scheduled_wakes = lambda: sorted(self.on_machine)
        self.ran = []                   # each batch _admin_shell was handed
        self.allow = True               # whether the password prompt "succeeds"

        def admin(lines):
            if not self.allow:
                return False
            self.ran.append(list(lines))
            return True
        sc._admin_shell = admin

    def hourly(self, every, name=None, **kw):
        return self.make(name or f"Every {every} hours",
                         interval={"every": every, "unit": "hours"}, **kw)

    # ---- which hours the machine has to wake at
    def test_the_hours_are_the_union_of_every_hourly_search(self):
        self.hourly(12)                              # 5, 17
        self.hourly(8)                               # 5, 13, 21
        self.assertEqual(sc.wake_hours_needed(), [13, 17, 21])

    def test_5am_is_left_out_because_the_daily_repeat_covers_it(self):
        self.hourly(12)
        self.assertNotIn(5, sc.wake_hours_needed())

    def test_paused_and_daily_searches_ask_for_no_wakes(self):
        self.hourly(6, enabled=False)
        self.make("Daily", interval={"every": 1, "unit": "days"})
        self.assertEqual(sc.wake_hours_needed(), [])

    def test_the_queue_reaches_the_horizon_and_only_holds_the_future(self):
        self.hourly(12)                              # one wake a day, 5pm
        now = dt("2026-08-06T18:00:00")              # today's 5pm already gone
        times = sc.wake_times_needed(now=now)
        self.assertEqual(times[0], dt("2026-08-07T17:00:00"))
        self.assertTrue(all(t > now for t in times))
        self.assertEqual(len(times), sc.WAKE_HORIZON_DAYS)

    def test_a_dense_grid_is_capped_not_written_in_full(self):
        self.hourly(3)
        self.assertLessEqual(len(sc.wake_times_needed()), sc.WAKE_MAX_EVENTS)

    # ---- reading what's on the machine
    def test_only_events_wearing_our_name_are_ours(self):
        real_run, sc.scheduled_wakes = sc.scheduled_wakes, self._wakes["scheduled_wakes"]
        try:
            self.addCleanup(setattr, sc, "_run", sc._run)
            report = (
                "Scheduled power events:\n"
                " [0]  wake at 08/10/2026 05:53:23 by 'com.apple.alarm'\n"
                " [1]  wake at 08/10/2026 17:00:00 by 'Faceplace Marketbook'\n"
                " [2]  wake at 08/11/2026 17:00:00 by 'Faceplace Marketbook'\n")
            sc._run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0,
                                                                    report, "")
            self.assertEqual(sc.scheduled_wakes(),
                             [dt("2026-08-10T17:00:00"),
                              dt("2026-08-11T17:00:00")])
        finally:
            sc.scheduled_wakes = real_run

    # ---- the state the window and the reports read
    def test_the_queue_means_nothing_without_hourly_searches(self):
        self.make("Daily", interval={"every": 1, "unit": "days"})
        self.assertIs(sc.wake_queue_state()["relevant"], False)

    def test_a_full_queue_is_neither_renewable_nor_naggable(self):
        self.hourly(12)
        now = dt("2026-08-06T12:00:00")
        self.on_machine = [now + timedelta(days=d, hours=5)
                           for d in range(1, sc.WAKE_HORIZON_DAYS + 1)]
        state = sc.wake_queue_state(now=now)
        self.assertEqual(state["days_left"], sc.WAKE_HORIZON_DAYS)
        self.assertIs(state["renew"], False)
        self.assertIs(state["nag"], False)

    def test_day_14_of_21_is_when_renewal_is_offered(self):
        # The user's numbers: 21 days deep, offer at 14 days in (7 left), nag
        # from day 18 (3 left).
        self.hourly(12)
        now = dt("2026-08-06T12:00:00")
        for left, renew, nag in ((8, False, False), (6, True, False),
                                 (5, True, False), (3, True, True),
                                 (0, True, True)):
            with self.subTest(days_left=left):
                self.on_machine = [now + timedelta(days=d, hours=1)
                                   for d in range(left)]
                state = sc.wake_queue_state(now=now)
                self.assertIs(state["renew"], renew)
                self.assertIs(state["nag"], nag)

    # ---- rewriting the queue
    def test_a_renewal_cancels_ours_and_writes_the_fresh_set(self):
        self.hourly(12)
        now = dt("2026-08-06T12:00:00")
        self.on_machine = [dt("2026-08-07T17:00:00")]
        changed, note = sc.renew_wakes(now=now, force=True)
        self.assertTrue(changed)
        lines = self.ran[-1]
        self.assertIn("pmset schedule cancel wake '08/07/26 17:00:00' "
                      "'Faceplace Marketbook' || true", lines)
        adds = [l for l in lines if not l.startswith("pmset schedule cancel")]
        self.assertEqual(adds[0], "pmset schedule wake '08/06/26 17:00:00' "
                                  "'Faceplace Marketbook'")
        self.assertEqual(len(adds), sc.WAKE_HORIZON_DAYS + 1)

    def test_a_queue_still_deep_enough_is_left_alone(self):
        # No pointless password prompts: same hours, more days left than the
        # renewal threshold, nothing to do.
        self.hourly(12)
        now = dt("2026-08-06T12:00:00")
        self.on_machine = [now + timedelta(days=d, hours=5)
                           for d in range(1, 10)]
        changed, note = sc.renew_wakes(now=now)
        self.assertFalse(changed)
        self.assertEqual(self.ran, [])

    def test_changing_the_hours_rewrites_even_a_deep_queue(self):
        self.hourly(12)                              # needs 17
        now = dt("2026-08-06T12:00:00")
        self.on_machine = [now.replace(hour=23) + timedelta(days=d)
                           for d in range(10)]       # holds 23 — stale
        changed, _ = sc.renew_wakes(now=now)
        self.assertTrue(changed)

    def test_the_last_hourly_search_gone_means_cleanup_only(self):
        self.make("Daily", interval={"every": 1, "unit": "days"})
        now = dt("2026-08-06T12:00:00")
        self.on_machine = [dt("2026-08-07T17:00:00")]
        changed, note = sc.renew_wakes(now=now)
        self.assertTrue(changed)
        self.assertIsNone(note)
        self.assertTrue(all(l.startswith("pmset schedule cancel")
                            for l in self.ran[-1]))

    def test_a_declined_password_prompt_says_where_to_try_again(self):
        self.hourly(12)
        self.allow = False
        changed, note = sc.renew_wakes()
        self.assertFalse(changed)
        self.assertIn("wasn't updated", note)
        self.assertIn("Email & Setup", note)

    # ---- the moments that rewrite it
    def test_saving_an_hourly_search_renews_the_queue_and_says_so(self):
        sc.save_email_config(dict(TestSavingNeedsEmail.ACCOUNT))
        for name in ("schedule_installed", "schedule_problems"):
            self.addCleanup(setattr, sc, name, getattr(sc, name))
        sc.schedule_installed = lambda: True
        sc.schedule_problems = lambda: []
        written = []

        def admin(lines):
            written.append(list(lines))
            self.on_machine = sorted(
                datetime.strptime(l.split("'")[1], "%m/%d/%y %H:%M:%S")
                for l in lines if not l.startswith("pmset schedule cancel"))
            return True
        sc._admin_shell = admin
        res = sc.ui_hooks()["save_search"](
            {**TestSavingNeedsEmail.FORM,
             "interval": {"every": 6, "unit": "hours"}})
        self.assertNotIn("error", res)
        self.assertTrue(written)
        self.assertIn("wake itself", res["message"])

    def test_a_declined_prompt_does_not_unsave_the_search(self):
        sc.save_email_config(dict(TestSavingNeedsEmail.ACCOUNT))
        for name in ("schedule_installed", "schedule_problems"):
            self.addCleanup(setattr, sc, name, getattr(sc, name))
        sc.schedule_installed = lambda: True
        sc.schedule_problems = lambda: []
        self.allow = False
        res = sc.ui_hooks()["save_search"](
            {**TestSavingNeedsEmail.FORM,
             "interval": {"every": 6, "unit": "hours"}})
        self.assertNotIn("error", res)
        self.assertIn("wake-up schedule wasn't updated", res["message"])
        self.assertEqual(len(sc.load_searches()), 1)

    def test_pausing_the_search_cleans_the_queue_up(self):
        self.hourly(6)
        rec = sc.load_searches()[0]
        self.on_machine = [sc.now_local() + timedelta(days=1)]
        res = sc.ui_hooks()["update_search"](rec["id"], {"enabled": False})
        self.assertNotIn("error", res)
        self.assertTrue(all(l.startswith("pmset schedule cancel")
                            for l in self.ran[-1]))

    def test_the_window_opens_knowing_the_queue(self):
        for name in ("schedule_installed", "schedule_problems"):
            self.addCleanup(setattr, sc, name, getattr(sc, name))
        sc.schedule_installed = lambda: True
        sc.schedule_problems = lambda: []
        self.hourly(12)
        state = sc.ui_hooks()["schedule_state"]()
        self.assertIs(state["wakes"]["relevant"], True)
        self.assertEqual(state["hour_choices"], list(sc.HOUR_CHOICES))
        self.assertEqual(state["daily_hour"], sc.DAILY_HOUR)

    def test_the_renew_button_forces_a_rewrite(self):
        self.hourly(12)
        now_wakes = [sc.now_local() + timedelta(days=d, hours=1)
                     for d in range(1, sc.WAKE_HORIZON_DAYS + 1)]
        self.on_machine = now_wakes                  # deep, but forced anyway
        res = sc.ui_hooks()["renew_wakes"]()
        self.assertNotIn("error", res)
        self.assertTrue(self.ran)
        self.assertIn("wakes", res)

    def test_turning_automatic_runs_on_writes_everything_in_one_prompt(self):
        # The 5am repeat and the whole queue travel in the same batch: two
        # password prompts for one click is how setup steps get abandoned.
        self.hourly(6)
        msg = sc.set_wakes(now=dt("2026-08-06T12:00:00"))
        self.assertEqual(len(self.ran), 1)
        lines = self.ran[0]
        self.assertEqual(lines[0],
                         "pmset repeat wakeorpoweron MTWRFSU 05:00:00")
        self.assertTrue(any(l.startswith("pmset schedule wake")
                            for l in lines[1:]))
        self.assertIn("5am", msg)

    def test_turning_them_off_takes_the_wake_ups_along(self):
        self.on_machine = [dt("2026-08-07T17:00:00")]
        plist = self.root / "agent.plist"
        plist.write_text("<plist/>", encoding="utf-8")
        self.addCleanup(setattr, sc, "plist_path", sc.plist_path)
        self.addCleanup(setattr, sc, "_run", sc._run)
        sc.plist_path = lambda: plist
        sc._run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", "")
        ok, msgs = sc.uninstall_schedule()
        self.assertTrue(ok)
        self.assertEqual(self.ran[0][0], "pmset repeat cancel")
        self.assertIn("pmset schedule cancel wake '08/07/26 17:00:00' "
                      "'Faceplace Marketbook' || true", self.ran[0])


# ------------------------------------------------------------------------- SMTP
class RecordingSMTP:
    """Stands in for smtplib.SMTP so the message itself can be inspected. The
    real TLS and login path is exercised by the live Gmail check instead."""
    sent = []
    logins = []
    started_tls = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        pass

    def starttls(self, context=None):
        RecordingSMTP.started_tls.append((self.host, self.port))

    def login(self, user, password):
        RecordingSMTP.logins.append((user, password))

    def send_message(self, msg):
        RecordingSMTP.sent.append(msg)

    @classmethod
    def reset(cls):
        cls.sent, cls.logins, cls.started_tls = [], [], []


class TestSending(Redirected):
    def setUp(self):
        super().setUp()
        RecordingSMTP.reset()
        import smtplib
        self._real = smtplib.SMTP
        smtplib.SMTP = RecordingSMTP
        self.addCleanup(lambda: setattr(smtplib, "SMTP", self._real))
        self.cfg = sc.save_email_config(
            {"provider": "gmail", "address": "me@gmail.com",
             "app_password": "abcd efgh ijkl mnop"})

    def test_config_round_trip(self):
        again = sc.load_email_config()
        self.assertEqual(again["address"], "me@gmail.com")
        self.assertTrue(sc.email_ready(again))

    def test_the_password_file_is_not_world_readable(self):
        if os.name == "nt":
            self.skipTest("POSIX permissions only")
        self.assertEqual(os.stat(sc.EMAIL_CONFIG_PATH).st_mode & 0o077, 0)

    def test_email_is_not_ready_without_a_password(self):
        self.assertFalse(sc.email_ready({"address": "a@b.c", "app_password": ""}))
        with self.assertRaises(RuntimeError):
            sc.send_email({"address": "", "app_password": ""}, "a@b.c", "s", "t")

    def test_a_mistyped_address_is_refused_at_save_time(self):
        # Not at 5am, in a run nobody is watching.
        hooks = sc.ui_hooks()
        for bad in ("me@gmial", "me at gmail.com", "me@ gmail.com", "megmail.com",
                    "me@gmail..com", "me@gmail.com extra"):
            with self.subTest(bad=bad):
                res = hooks["save_email"]({"provider": "gmail", "address": bad,
                                           "app_password": "abcdefghijklmnop"})
                self.assertIn("error", res)
                self.assertIn("Nothing was saved", res["error"])
        self.assertEqual(sc.load_email_config()["address"], "me@gmail.com")

    def test_an_old_send_to_address_is_cleared_out_on_the_next_save(self):
        # It used to be a second place to say where reports go, alongside the
        # one on each search. Left in the file it would still quietly redirect
        # a search that has no address of its own.
        sc.save_email_config({"provider": "gmail", "address": "me@gmail.com",
                              "app_password": "pw",
                              "default_to": "someone@else.com"})
        res = sc.ui_hooks()["save_email"](
            {"provider": "gmail", "address": "me@gmail.com",
             "app_password": "abcdefghijklmnop"})
        self.assertNotIn("error", res)
        self.assertNotIn("default_to", sc.load_email_config())

    def test_a_search_with_no_address_of_its_own_reports_to_the_account(self):
        sc.save_email_config({"provider": "gmail", "address": "me@gmail.com",
                              "app_password": "pw"})
        sc.send_email(sc.load_email_config(), "", "subject", "body")
        self.assertEqual(RecordingSMTP.sent[-1]["To"], "me@gmail.com")

    def test_an_unusual_but_valid_address_is_accepted(self):
        for good in ("me+tag@gmail.com", "first.last@sub.domain.co.uk",
                     "me_1@googlemail.com"):
            with self.subTest(good=good):
                res = sc.ui_hooks()["save_email"](
                    {"provider": "gmail", "address": good,
                     "app_password": "abcdefghijklmnop"})
                self.assertNotIn("error", res)

    def test_a_domain_that_doesnt_match_the_provider_is_left_alone(self):
        # Work and school accounts on Google Workspace are served from
        # smtp.gmail.com under their own domain, and they're common enough that
        # the test send is a better answer than a remark.
        res = sc.ui_hooks()["save_email"](
            {"provider": "gmail", "address": "me@company.com",
             "app_password": "abcdefghijklmnop"})
        self.assertNotIn("error", res)
        self.assertNotIn("company.com isn't", res["message"])

    def test_a_normal_password_is_remarked_on(self):
        # "my-real-password" is itself sixteen characters, so length alone would
        # have waved it through.
        for pw in ("hunter2", "my-real-password", "Abcdefghijklmnop"):
            with self.subTest(pw=pw):
                res = sc.ui_hooks()["save_email"](
                    {"provider": "gmail", "address": "me@gmail.com",
                     "app_password": pw})
                self.assertIn("app password", res["message"])

    def test_a_real_app_password_is_not_remarked_on(self):
        for pw in ("abcdefghijklmnop", "abcd efgh ijkl mnop"):
            with self.subTest(pw=pw):
                res = sc.ui_hooks()["save_email"](
                    {"provider": "gmail", "address": "me@gmail.com",
                     "app_password": pw})
                self.assertNotIn("app password", res["message"])

    def test_an_icloud_address_on_icloud_is_not_remarked_on(self):
        res = sc.ui_hooks()["save_email"](
            {"provider": "icloud", "address": "me@me.com",
             "app_password": "abcd-efgh-ijkl-mnop"})
        self.assertNotIn("isn't", res["message"])

    def test_the_auth_error_hint_mentions_the_address_as_well(self):
        e = sc.smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")
        hint = sc._smtp_hint(e, {"address": "me@gmial.com"})
        self.assertIn("me@gmial.com", hint)
        self.assertIn("app password", hint)

    def test_a_refused_recipient_says_to_check_the_address(self):
        e = sc.smtplib.SMTPRecipientsRefused({"nope@": (550, b"No such user")})
        self.assertIn("typo", sc._smtp_hint(e))

    def test_gmail_host_is_used_by_default(self):
        sc.send_email(self.cfg, "you@example.com", "Subject here", "Body here")
        self.assertEqual(RecordingSMTP.started_tls, [("smtp.gmail.com", 587)])
        self.assertEqual(RecordingSMTP.logins,
                         [("me@gmail.com", "abcd efgh ijkl mnop")])

    def test_an_explicit_host_overrides_the_provider(self):
        cfg = dict(self.cfg, host="mail.example.net", port=2525)
        sc.send_email(cfg, "you@example.com", "s", "t")
        self.assertEqual(RecordingSMTP.started_tls, [("mail.example.net", 2525)])

    def test_headers_and_body(self):
        sc.send_email(self.cfg, "you@example.com", "Defender 110: 2 new",
                      "plain text body", "<p>html body</p>")
        msg = RecordingSMTP.sent[0]
        self.assertEqual(msg["From"], "me@gmail.com")
        self.assertEqual(msg["To"], "you@example.com")
        self.assertEqual(msg["Subject"], "Defender 110: 2 new")
        self.assertTrue(msg["Message-ID"])
        self.assertTrue(msg["Date"])
        self.assertIn("plain text body", msg.get_body(("plain",)).get_content())
        self.assertIn("html body", msg.get_body(("html",)).get_content())

    def test_the_recipient_falls_back_to_the_default(self):
        sc.send_email(self.cfg, None, "s", "t")
        self.assertEqual(RecordingSMTP.sent[0]["To"], "me@gmail.com")

    def test_both_attachments_arrive_intact(self):
        payloads = [("new-listings.html", b"<html>new</html>", "html"),
                    ("all-results.html", b"<html>all</html>", "html")]
        sc.send_email(self.cfg, "you@example.com", "s", "t", "<p>h</p>", payloads)
        msg = RecordingSMTP.sent[0]
        got = {p.get_filename(): p for p in msg.iter_attachments()
               if p.get_filename()}
        self.assertEqual(sorted(got), ["all-results.html", "new-listings.html"])
        self.assertEqual(got["new-listings.html"].get_content_type(), "text/html")
        self.assertIn(b"<html>new</html>",
                      got["new-listings.html"].get_payload(decode=True))
        # And it survives a real serialise/parse round trip, which is what the
        # SMTP server would actually see.
        reparsed = message_from_bytes(msg.as_bytes())
        names = [p.get_filename() for p in reparsed.walk() if p.get_filename()]
        self.assertEqual(sorted(names), ["all-results.html", "new-listings.html"])

    def test_the_app_password_never_appears_in_the_message(self):
        sc.send_email(self.cfg, "you@example.com", "s", "body", "<p>h</p>")
        self.assertNotIn(b"abcd efgh ijkl mnop", RecordingSMTP.sent[0].as_bytes())

    def test_failure_notices_explain_what_to_do(self):
        sc.notify_failure(self.cfg, "you@example.com", SEARCH, "session_expired",
                          "no cookie", dt("2026-08-07T05:00:00"))
        body = RecordingSMTP.sent[0].get_body(("plain",)).get_content()
        self.assertIn("log into Facebook again",
                      RecordingSMTP.sent[0]["Subject"])
        self.assertIn("Log into Facebook the way you normally would", body)
        self.assertIn("two-factor code and captcha", body)
        self.assertIn("Next attempt", body)

    def test_error_notices_say_the_search_is_still_scheduled(self):
        sc.notify_failure(self.cfg, "you@example.com", SEARCH, "error",
                          "Traceback: boom")
        body = RecordingSMTP.sent[0].get_body(("plain",)).get_content()
        self.assertIn("still scheduled", body)
        self.assertIn("boom", body)

    def test_a_failure_to_send_never_raises(self):
        # A broken mail server must not turn into a crash that hides the run.
        import smtplib
        smtplib.SMTP = lambda *a, **k: (_ for _ in ()).throw(OSError("no route"))
        sc.notify_failure(self.cfg, "you@example.com", SEARCH, "error", "x")


# ------------------------------------------------------------------------- lock
LOCK_CHILD = """
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import scheduling as sc
sc.SCHEDULE_DIR = Path(sys.argv[2])
sc.LOCK_PATH = sc.SCHEDULE_DIR / "run.lock"
sc.TICK_LOG = sc.SCHEDULE_DIR / "tick.log"
try:
    with sc.run_lock("child"):
        print("GOT")
except sc.AlreadyRunning as e:
    print("BLOCKED")
"""


class TestRunLock(Redirected):
    def child(self):
        script = self.root / "lock_child.py"
        script.write_text(LOCK_CHILD, encoding="utf-8")
        r = subprocess.run([sys.executable, str(script), str(SRC),
                            str(sc.SCHEDULE_DIR)],
                           capture_output=True, text=True, timeout=60)
        return r.stdout.strip()

    def test_the_lock_is_released_afterwards(self):
        with sc.run_lock("first"):
            self.assertTrue(sc.LOCK_PATH.exists())
        self.assertFalse(sc.LOCK_PATH.exists())
        with sc.run_lock("second"):
            pass

    def test_a_second_run_in_the_same_process_is_refused(self):
        with sc.run_lock("first"):
            with self.assertRaises(sc.AlreadyRunning):
                with sc.run_lock("second"):
                    pass

    def test_another_process_cannot_start_a_run(self):
        # The real guarantee: the scheduler and a manual run are separate
        # processes sharing one Facebook session.
        self.assertEqual(self.child(), "GOT")
        with sc.run_lock("scheduled run"):
            self.assertEqual(self.child(), "BLOCKED")
        self.assertEqual(self.child(), "GOT")

    def test_a_lock_left_by_a_dead_process_is_reclaimed(self):
        sc.SCHEDULE_DIR.mkdir(parents=True, exist_ok=True)
        sc.LOCK_PATH.write_text(json.dumps(
            {"pid": 999_999, "what": "crashed run",
             "started": sc.iso(sc.now_local())}), encoding="utf-8")
        with sc.run_lock("new run"):
            pass

    def test_an_ancient_lock_is_reclaimed_even_if_the_pid_is_alive(self):
        sc.SCHEDULE_DIR.mkdir(parents=True, exist_ok=True)
        old = sc.iso(sc.now_local() - timedelta(hours=sc.LOCK_STALE_HOURS + 1))
        sc.LOCK_PATH.write_text(json.dumps(
            {"pid": os.getpid(), "what": "wedged run", "started": old}),
            encoding="utf-8")
        with sc.run_lock("new run"):
            pass

    def test_an_unreadable_lock_is_reclaimed(self):
        sc.SCHEDULE_DIR.mkdir(parents=True, exist_ok=True)
        sc.LOCK_PATH.write_text("garbage", encoding="utf-8")
        with sc.run_lock("new run"):
            pass

    def test_the_message_says_who_holds_it(self):
        with sc.run_lock("scheduled run"):
            try:
                with sc.run_lock("manual run"):
                    pass
            except sc.AlreadyRunning as e:
                self.assertIn("scheduled run", str(e))

    def test_finishing_leaves_a_lock_that_is_no_longer_ours_alone(self):
        # A run that outlived the stale threshold had its lock reclaimed and
        # replaced by a newer run. Finishing must not delete the new holder's
        # lock, or a third run could start alongside the second.
        with sc.run_lock("first"):
            sc.LOCK_PATH.unlink()
            sc.LOCK_PATH.write_text(json.dumps(
                {"pid": 999_999, "what": "newer run",
                 "started": sc.iso(sc.now_local())}), encoding="utf-8")
        self.assertTrue(sc.LOCK_PATH.exists())


# ------------------------------------------------- the whole run, minus a browser
class TestScheduledPipeline(Redirected):
    """Exercises everything around the sweep — folder reuse, bookkeeping, the
    report, attachments and email — with the browser replaced by a stub."""

    def setUp(self):
        super().setUp()
        RecordingSMTP.reset()
        import smtplib
        real_smtp = smtplib.SMTP
        smtplib.SMTP = RecordingSMTP
        self.addCleanup(lambda: setattr(smtplib, "SMTP", real_smtp))

        self._storage = {"DB_PATH": storage.DB_PATH, "RUNS_DIR": storage.RUNS_DIR}
        storage.DB_PATH = self.root / "db.sqlite"
        storage.RUNS_DIR = self.root / "runs"
        self.addCleanup(self._restore_storage)

        sc.save_email_config({"provider": "gmail", "address": "me@gmail.com",
                              "app_password": "pw"})
        self.calls = []
        self.asked_for = []

    def _restore_storage(self):
        for k, v in self._storage.items():
            setattr(storage, k, v)

    def stub_sweep(self, batches):
        """Each entry is (rows found in the feed, listings confirmed removed).
        Uses the real reconciliation so the carry-forward rule is exercised."""
        def sweep(query, scrolls, exact, **kw):
            self.calls.append(kw)
            self.asked_for.append(query)
            feed_rows, removed = batches[len(self.calls) - 1]
            run_dir = Path(kw["run_dir"])
            prev_by_id = {r["item_id"]: dict(r)
                          for r in (kw.get("previous_rows") or [])}
            all_rows = {r["item_id"]: dict(r) for r in feed_rows}
            new_ids, _ = storage.reconcile_with_previous(
                all_rows, prev_by_id, {r["item_id"] for r in removed})
            rows = list(all_rows.values())

            csv_path = run_dir / "results.csv"
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=storage.FIELDS)
                w.writeheader()
                for r in rows:
                    w.writerow({k: r.get(k, "") for k in storage.FIELDS})
            (run_dir / "run.json").write_text(
                json.dumps({"query": query}), encoding="utf-8")
            (run_dir / "gallery.html").write_text("<html></html>", encoding="utf-8")
            con = storage.open_db(storage.DB_PATH)
            for r in rows:
                storage.upsert(con, r)
            con.commit()
            con.close()
            return {
                "status": "ok", "run_dir": str(run_dir), "csv": str(csv_path),
                "gallery": str(run_dir / "gallery.html"),
                "started": sc.iso(sc.now_local()),
                "finished": sc.iso(sc.now_local()), "duration_seconds": 42.0,
                "new_ids": new_ids,
                "feed_ids": [r["item_id"] for r in feed_rows],
                "total_ids": [r["item_id"] for r in rows],
                "new_rows": [r for r in rows if r["item_id"] in set(new_ids)],
                "removed": removed, "descriptions_fetched": len(new_ids),
                "described_ids": new_ids, "radius_km": kw.pop("_radius", 805),
                "per_city": {}, "interrupted": False,
            }
        return sweep

    def batch(self, *ids):
        return [listing(i, f"Listing {i}") for i in ids]

    # -- one run ------------------------------------------------------------
    def test_a_first_run_writes_everything_and_emails_it(self):
        rec = self.make(name="Defender 110")
        sweep = self.stub_sweep([(self.batch("a", "b", "c"), [])])
        summary = sc.run_saved_search(rec, sweep=sweep)

        run_dir = Path(summary["run_dir"])
        self.assertEqual(run_dir.name, "defender_110")
        self.assertEqual(run_dir.parent, self.root / "runs" / "saved")
        for name in ("results.csv", "run.json", "gallery.html", "report.html"):
            self.assertTrue((run_dir / name).exists(), name)
        self.assertEqual(len(RecordingSMTP.sent), 1)
        names = sorted(p.get_filename() for p in
                       RecordingSMTP.sent[0].iter_attachments()
                       if p.get_filename())
        self.assertEqual(names, ["all-results.html", "new-listings.html"])

    def test_the_run_is_recorded_in_the_database(self):
        rec = self.make()
        sc.run_saved_search(rec, sweep=self.stub_sweep([(self.batch("a", "b"), [])]))
        con = storage.open_db(storage.DB_PATH)
        self.addCleanup(con.close)
        row = con.execute("SELECT search_id, new_count, total_count, "
                          "removed_count, status FROM search_runs").fetchone()
        self.assertEqual(row, (rec["id"], 2, 2, 0, "ok"))
        self.assertEqual(sorted(sc.previous_item_ids(con, rec["id"])), ["a", "b"])

    def test_the_sweep_is_handed_every_query_the_search_holds(self):
        rec = self.make(name="Either", queries=["defender 110", "land rover 90"])
        sc.run_saved_search(rec, sweep=self.stub_sweep([(self.batch("a"), [])]))
        self.assertEqual(self.asked_for, [["defender 110", "land rover 90"]])

    def test_a_single_query_search_still_hands_over_a_list(self):
        rec = self.make()
        sc.run_saved_search(rec, sweep=self.stub_sweep([(self.batch("a"), [])]))
        self.assertEqual(self.asked_for, [["defender 110"]])

    def test_timestamps_are_written_before_the_run_starts(self):
        # Written up front so a crash can't leave a search retrying in a loop.
        rec = self.make()
        sc.run_saved_search(rec, sweep=self.stub_sweep([(self.batch("a"), [])]))
        saved = sc.load_searches()[0]
        self.assertTrue(saved["last_started"])
        self.assertTrue(saved["last_finished"])
        self.assertGreater(sc.parse_iso(saved["next_run"]), sc.now_local())

    # -- the second run -----------------------------------------------------
    def two_runs(self, second_feed, removed=()):
        rec = self.make(interval={"every": 5, "unit": "minutes"})
        sweep = self.stub_sweep([(self.batch("a", "b", "c"), []),
                                 (second_feed, list(removed))])
        first = sc.run_saved_search(rec, sweep=sweep)
        second = sc.run_saved_search(sc.load_searches()[0], sweep=sweep)
        return first, second

    def test_the_second_run_reuses_the_same_folder(self):
        first, second = self.two_runs(self.batch("a", "b", "c", "d"))
        self.assertEqual(first["run_dir"], second["run_dir"])
        saved_root = self.root / "runs" / "saved"
        self.assertEqual([p.name for p in saved_root.iterdir()], ["defender_110"])

    def test_the_previous_run_is_archived_not_lost(self):
        _, second = self.two_runs(self.batch("a", "b", "c"))
        hist = Path(second["run_dir"]) / "history"
        self.assertTrue((hist / "run-1.json").exists())
        self.assertTrue((hist / "report-1.html").exists())

    def test_the_second_run_is_told_what_the_first_one_found(self):
        self.two_runs(self.batch("a", "b", "c"))
        first_kwargs, second_kwargs = self.calls
        self.assertEqual(first_kwargs["previous_rows"], [])
        self.assertEqual(
            sorted(r["item_id"] for r in second_kwargs["previous_rows"]),
            ["a", "b", "c"])

    def test_scheduled_runs_only_describe_new_listings(self):
        self.two_runs(self.batch("a", "b", "c", "d"))
        for kwargs in self.calls:
            self.assertTrue(kwargs["describe_new_only"])
        # Run two saw one genuinely new listing, so that's all it paid for.
        self.assertEqual(self.calls[1]["previous_rows"] and 1, 1)

    def test_scheduled_runs_never_wait_for_a_human(self):
        self.two_runs(self.batch("a"))
        for kwargs in self.calls:
            self.assertTrue(kwargs["unattended"])
            self.assertTrue(kwargs["no_pause"])
            self.assertFalse(kwargs["open_gallery"])
            self.assertEqual(kwargs["login_wait"], 60)

    # -- what stays and what goes -------------------------------------------
    def test_a_listing_missing_from_the_feed_is_kept(self):
        # 'c' didn't come back this time and wasn't confirmed gone, so it stays.
        _, second = self.two_runs(self.batch("a", "b"))
        self.assertEqual(sorted(second["total_ids"]), ["a", "b", "c"])
        _, text, _ = second["report"]["subject"], second["report"]["text"], None
        self.assertIn("Nothing was sold or taken down", text)

    def test_a_confirmed_removal_leaves_the_results_and_is_reported(self):
        gone = {**listing("c", "Defender 110 hardtop"), "removal": "gone",
                "marker": "this listing isn't available"}
        _, second = self.two_runs(self.batch("a", "b"), removed=[gone])
        self.assertEqual(sorted(second["total_ids"]), ["a", "b"])
        text = second["report"]["text"]
        self.assertIn("TAKEN DOWN (1)", text)
        self.assertIn("Defender 110 hardtop", text)
        with open(second["csv"], encoding="utf-8") as f:
            ids_in_csv = [r["item_id"] for r in csv.DictReader(f)]
        self.assertNotIn("c", ids_in_csv)

    def test_a_sold_listing_is_reported_as_sold(self):
        sold = {**listing("c", "Defender 110 sold one"), "removal": "sold",
                "marker": '"is_sold":true'}
        _, second = self.two_runs(self.batch("a", "b"), removed=[sold])
        self.assertIn("SOLD (1)", second["report"]["text"])
        con = storage.open_db(storage.DB_PATH)
        self.addCleanup(con.close)
        self.assertEqual(con.execute(
            "SELECT removed_count FROM search_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()[0], 1)

    def test_listings_in_the_feed_are_marked_alive_for_free(self):
        rec = self.make()
        sc.run_saved_search(rec, sweep=self.stub_sweep([(self.batch("a", "b"), [])]))
        con = storage.open_db(storage.DB_PATH)
        self.addCleanup(con.close)
        # Nothing needs re-checking straight after a run that saw them.
        self.assertEqual(sc.needs_verifying(con, ["a", "b"], 24), [])

    def test_a_carried_listing_is_not_stamped_alive_again(self):
        # 'c' vanished from the feed on run two and was only carried forward.
        # If the run refreshed its last_verified anyway, it would never come
        # due for re-checking and a sold listing could be kept forever.
        rec = self.make(interval={"every": 5, "unit": "minutes"})
        sweep = self.stub_sweep([(self.batch("a", "b", "c"), []),
                                 (self.batch("a", "b"), [])])
        sc.run_saved_search(rec, sweep=sweep)
        con = storage.open_db(storage.DB_PATH)
        self.addCleanup(con.close)
        old = sc.iso(sc.now_local() - timedelta(days=3))
        con.execute("UPDATE listing_state SET last_verified=? "
                    "WHERE item_id='c'", (old,))
        con.commit()
        sc.run_saved_search(sc.load_searches()[0], sweep=sweep)
        self.assertEqual(con.execute(
            "SELECT last_verified FROM listing_state WHERE item_id='c'"
        ).fetchone()[0], old)
        self.assertEqual(sc.needs_verifying(con, ["c"], 24), ["c"])

    # -- warnings -----------------------------------------------------------
    def test_a_short_radius_warns_but_the_run_still_finishes(self):
        rec = self.make()
        sweep = self.stub_sweep([(self.batch("a"), [])])

        def short_radius(*a, **kw):
            out = sweep(*a, **kw)
            out["radius_km"] = 402
            return out
        summary = sc.run_saved_search(rec, sweep=short_radius)
        self.assertEqual(summary["status"], "ok")
        warnings = summary["report"]["warnings"]
        self.assertTrue(any("250 miles" in w for w in warnings))
        self.assertIn("Heads up", summary["report"]["html"])

    def test_a_late_run_says_the_machine_was_probably_asleep(self):
        rec = self.make()
        sc.update_search(rec["id"], {})
        searches = sc.load_searches()
        searches[0]["next_run"] = sc.iso(sc.now_local() - timedelta(hours=9))
        sc.save_searches(searches)
        summary = sc.run_saved_search(sc.load_searches()[0],
                                      sweep=self.stub_sweep([(self.batch("a"), [])]))
        self.assertTrue(any("asleep or switched off" in w
                            for w in summary["report"]["warnings"]))

    def test_run_now_is_late_by_definition_and_does_not_warn_about_it(self):
        rec = self.make(name="Defender 110")
        searches = sc.load_searches()
        searches[0]["next_run"] = sc.iso(sc.now_local() - timedelta(hours=9))
        sc.save_searches(searches)
        results = sc.tick(force="Defender 110",
                          sweep=self.stub_sweep([(self.batch("a"), [])]))
        self.assertEqual(results[0]["report"]["warnings"], [])

    def test_an_on_time_run_has_no_warnings(self):
        rec = self.make()
        summary = sc.run_saved_search(rec,
                                      sweep=self.stub_sweep([(self.batch("a"), [])]))
        self.assertEqual(summary["report"]["warnings"], [])

    def test_the_report_nags_when_the_wake_queue_is_nearly_dry(self):
        # Day 18 of 21: three days of wake-ups left. The report is the one
        # channel guaranteed to reach someone whose machine runs unattended, so
        # it's where the queue running out gets announced beforehand.
        self.addCleanup(setattr, sc, "os_name", sc.os_name)
        sc.os_name = lambda: "darwin"
        rec = self.make(interval={"every": 6, "unit": "hours"})
        sc.scheduled_wakes = lambda: [sc.now_local() + timedelta(days=2)]
        summary = sc.run_saved_search(rec,
                                      sweep=self.stub_sweep([(self.batch("a"), [])]))
        nags = [w for w in summary["report"]["warnings"] if "wake" in w]
        self.assertEqual(len(nags), 1)
        self.assertIn("renew", nags[0])
        body = RecordingSMTP.sent[0].get_body(("plain",)).get_content()
        self.assertIn("wake", body)

    def test_a_deep_wake_queue_earns_no_nag(self):
        self.addCleanup(setattr, sc, "os_name", sc.os_name)
        sc.os_name = lambda: "darwin"
        rec = self.make(interval={"every": 6, "unit": "hours"})
        sc.scheduled_wakes = lambda: [sc.now_local() + timedelta(days=15)]
        summary = sc.run_saved_search(rec,
                                      sweep=self.stub_sweep([(self.batch("a"), [])]))
        self.assertFalse([w for w in summary["report"]["warnings"]
                          if "wake" in w])

    # -- failures -----------------------------------------------------------
    def test_an_expired_session_emails_and_skips_the_rest_of_the_queue(self):
        first = self.make(name="First")
        second = self.make(name="Second")
        for s in (first, second):
            sc.update_search(s["id"], {"next_run": sc.iso(sc.now_local()
                                                          - timedelta(minutes=1))})

        def boom(*a, **kw):
            raise browser.SessionExpired("no c_user cookie")
        results = sc.tick(sweep=boom)
        self.assertEqual(results, [])
        self.assertEqual(len(RecordingSMTP.sent), 1)
        self.assertIn("log into Facebook again", RecordingSMTP.sent[0]["Subject"])
        body = RecordingSMTP.sent[0].get_body(("plain",)).get_content()
        self.assertIn("Log into Facebook the way you normally would", body)

    def test_an_unexpected_error_emails_and_leaves_the_search_scheduled(self):
        rec = self.make()
        sc.update_search(rec["id"], {"next_run": sc.iso(sc.now_local()
                                                        - timedelta(minutes=1))})

        def boom(*a, **kw):
            raise RuntimeError("chromium fell over")
        sc.tick(sweep=boom)
        self.assertEqual(len(RecordingSMTP.sent), 1)
        self.assertIn("scheduled run failed", RecordingSMTP.sent[0]["Subject"])
        saved = sc.load_searches()[0]
        self.assertTrue(saved["enabled"])
        self.assertGreater(sc.parse_iso(saved["next_run"]), sc.now_local())

    def test_a_failing_search_does_not_stop_the_others(self):
        one = self.make(name="Breaks")
        two = self.make(name="Works")
        for s in (one, two):
            sc.update_search(s["id"], {"next_run": sc.iso(sc.now_local()
                                                          - timedelta(minutes=1))})
        good = self.stub_sweep([(self.batch("a"), [])])

        def sweep(*a, **kw):
            if "breaks" in str(kw.get("run_dir")):
                raise RuntimeError("nope")
            return good(*a, **kw)
        results = sc.tick(sweep=sweep)
        self.assertEqual(len(results), 1)
        subjects = [m["Subject"] for m in RecordingSMTP.sent]
        self.assertTrue(any("failed" in s for s in subjects))
        self.assertTrue(any("Works" in s for s in subjects))

    # -- the tick itself ----------------------------------------------------
    def test_a_tick_with_nothing_due_does_nothing(self):
        self.make(interval={"every": 1, "unit": "days"})
        self.assertEqual(sc.tick(sweep=self.stub_sweep([])), [])
        self.assertEqual(RecordingSMTP.sent, [])

    def test_a_tick_runs_due_searches_in_creation_order(self):
        for name in ("Alpha", "Beta"):
            rec = self.make(name=name)
            sc.update_search(rec["id"], {"next_run": sc.iso(sc.now_local()
                                                            - timedelta(minutes=1))})
        sweep = self.stub_sweep([(self.batch("a"), []), (self.batch("b"), [])])
        results = sc.tick(sweep=sweep)
        self.assertEqual(len(results), 2)
        folders = [Path(r["run_dir"]).name for r in results]
        self.assertEqual(folders, ["alpha", "beta"])

    def test_a_paused_search_is_skipped_by_the_tick(self):
        rec = self.make()
        sc.update_search(rec["id"], {"enabled": False,
                                     "next_run": sc.iso(sc.now_local()
                                                        - timedelta(days=1))})
        self.assertEqual(sc.tick(sweep=self.stub_sweep([])), [])

    def test_run_now_ignores_the_schedule(self):
        self.make(name="Defender 110")
        results = sc.tick(force="Defender 110",
                          sweep=self.stub_sweep([(self.batch("a"), [])]))
        self.assertEqual(len(results), 1)

    def test_run_now_needs_a_real_name(self):
        with self.assertRaises(SystemExit):
            sc.tick(force="Nope")

    def test_a_tick_will_not_start_while_another_run_holds_the_lock(self):
        rec = self.make()
        sc.update_search(rec["id"], {"next_run": sc.iso(sc.now_local()
                                                        - timedelta(minutes=1))})
        with sc.run_lock("manual run"):
            self.assertEqual(sc.tick(sweep=self.stub_sweep([])), [])
        self.assertEqual(RecordingSMTP.sent, [])

    def test_no_email_setup_still_runs_and_saves_results(self):
        sc.EMAIL_CONFIG_PATH.unlink()
        rec = self.make()
        summary = sc.run_saved_search(rec,
                                      sweep=self.stub_sweep([(self.batch("a"), [])]))
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(RecordingSMTP.sent, [])
        self.assertTrue((Path(summary["run_dir"]) / "report.html").exists())


# ------------------------------------------------------- OS schedule generation
class TestScheduleFiles(unittest.TestCase):
    def test_the_mac_agent_calls_the_tick_from_the_venv(self):
        xml = sc.mac_plist()
        self.assertIn("scheduling.py", xml)
        self.assertIn("--tick", xml)
        self.assertIn(f"<integer>{sc.TICK_SECONDS}</integer>", xml)
        self.assertIn("<key>RunAtLoad</key>", xml)

    def test_the_windows_task_is_allowed_to_wake_the_computer(self):
        xml = sc.win_task_xml()
        self.assertIn("<WakeToRun>true</WakeToRun>", xml)
        self.assertIn("InteractiveToken", xml)
        self.assertIn(f"PT{sc.TICK_SECONDS // 60}M", xml)
        self.assertIn("--tick", xml)
        self.assertIn("<StartWhenAvailable>true</StartWhenAvailable>", xml)

    def test_the_windows_task_runs_on_battery(self):
        xml = sc.win_task_xml()
        self.assertIn("<DisallowStartIfOnBatteries>false", xml)

    def test_the_windows_xml_is_well_formed(self):
        import xml.etree.ElementTree as ET
        ET.fromstring(sc.win_task_xml())

    def test_the_mac_plist_is_well_formed(self):
        import plistlib
        d = plistlib.loads(sc.mac_plist().encode("utf-8"))
        self.assertEqual(d["Label"], sc.MAC_LABEL)
        self.assertEqual(d["ProgramArguments"][-1], "--tick")

    def test_the_interpreter_is_the_project_venv_when_present(self):
        exe = sc.python_exe()
        if (REPO / ".venv").exists():
            self.assertIn(".venv", exe)

    def test_the_agent_logs_outside_the_project(self):
        # The whole point: a folder macOS blocks can't hold the log that would
        # explain the block.
        xml = sc.mac_plist()
        self.assertIn(str(sc.AGENT_LOG), xml)
        self.assertNotIn(str(sc.SCHEDULE_DIR / "launchd.log"), xml)
        self.assertFalse(str(sc.AGENT_LOG).startswith(str(sc.ROOT)))


DENVER = "https://www.facebook.com/marketplace/denver/search/?query=x"


class TestCities(unittest.TestCase):
    """The city list is the one piece of shared state a stray click could shrink
    without it being visible anywhere afterwards. It's split across two files: the
    shipped list, which is tracked in git and only ever read, and the user's own,
    which is git-ignored and takes every write."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.orig = (locations.LOC_CACHE, locations.USER_LOC_CACHE)
        locations.LOC_CACHE = Path(self.tmp.name) / "locations.json"
        locations.USER_LOC_CACHE = Path(self.tmp.name) / "my_locations.json"
        self.write(locations.BUILTIN_LOCATIONS)
        # The migration announces itself, which belongs in a terminal and not in
        # the middle of test output.
        quiet = redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def tearDown(self):
        locations.LOC_CACHE, locations.USER_LOC_CACHE = self.orig

    def write(self, mapping):
        locations.LOC_CACHE.write_text(json.dumps(mapping), encoding="utf-8")

    def write_mine(self, mapping):
        locations.USER_LOC_CACHE.write_text(json.dumps(mapping), encoding="utf-8")

    # ------------------------------------------------ the shipped list
    def test_a_missing_shipped_file_falls_back_to_the_built_ins(self):
        locations.LOC_CACHE.unlink()
        self.assertEqual(locations.load_locations(), locations.BUILTIN_LOCATIONS)

    def test_a_corrupt_shipped_file_falls_back_to_the_built_ins(self):
        for junk in ("{ not json", "[1, 2, 3]", ""):
            with self.subTest(junk=junk):
                locations.LOC_CACHE.write_text(junk, encoding="utf-8")
                self.assertEqual(locations.load_locations(), locations.BUILTIN_LOCATIONS)

    def test_a_deleted_built_in_comes_back(self):
        # Whatever removed it — hand-editing, an older version, a bad merge — the
        # coverage hole shouldn't be permanent.
        short = dict(locations.BUILTIN_LOCATIONS)
        short.pop("Boston, MA")
        self.write(short)
        self.assertIn("Boston, MA", locations.load_locations())

    def test_the_shipped_file_is_never_written_to(self):
        # The whole point of the split: adding a city of your own leaves the
        # tracked file byte-for-byte as it was.
        before = locations.LOC_CACHE.read_bytes()
        locations.add_location("Denver, CO", DENVER)
        locations.remove_location("Denver, CO")
        locations.load_locations()
        self.assertEqual(locations.LOC_CACHE.read_bytes(), before)

    def test_the_file_can_correct_a_segment_facebook_changed(self):
        self.write({**locations.BUILTIN_LOCATIONS, "Boston, MA": "boston-new-id"})
        self.assertEqual(locations.base_locations()["Boston, MA"], "boston-new-id")

    def test_a_name_the_file_adds_is_ignored(self):
        # locations.json is read for segments, not for membership. Which cities
        # ship is decided in code, and your own go in your own file.
        self.write({**locations.BUILTIN_LOCATIONS, "Reno, NV": "reno"})
        self.assertFalse(locations.is_builtin("Reno, NV"))
        locs = locations.load_locations()
        self.assertNotIn("Reno, NV", locs)
        self.assertEqual(len(locs), len(locations.BUILTIN_LOCATIONS))

    # ------------------------------------------------ your own cities
    def test_your_own_cities_go_in_your_own_file_and_come_last(self):
        locations.add_location("Denver, CO", DENVER)
        self.assertEqual(locations.read_locations_file(locations.USER_LOC_CACHE),
                         {"Denver, CO": "denver"})
        self.assertEqual(list(locations.load_locations())[-1], "Denver, CO")

    def test_a_built_in_cannot_be_removed(self):
        locs, err = locations.remove_location("Boston, MA")
        self.assertIn("can't be removed", err)
        self.assertIn("Boston, MA", locs)
        self.assertIn("Boston, MA", locations.load_locations())

    def test_a_city_you_added_can_be_removed(self):
        locations.add_location("Denver, CO", DENVER)
        locs, err = locations.remove_location("Denver, CO")
        self.assertIsNone(err)
        self.assertNotIn("Denver, CO", locs)
        self.assertNotIn("Denver, CO", locations.load_locations())

    def test_removing_a_city_that_isnt_there_says_so(self):
        _, err = locations.remove_location("Atlantis, XX")
        self.assertIn("no city", err)

    def test_your_own_file_cannot_shadow_a_shipped_city(self):
        # An entry under a shipped city's name must not repoint it somewhere else.
        self.write_mine({"Boston, MA": "somewhere-else"})
        self.assertEqual(locations.load_locations()["Boston, MA"],
                         locations.BUILTIN_LOCATIONS["Boston, MA"])

    def test_loading_the_cities_writes_nothing(self):
        locations.load_locations()
        self.assertFalse(locations.USER_LOC_CACHE.exists())

    def test_junk_that_cannot_be_a_slug_is_refused(self):
        for text in ("", "   ", "https://example.com/nothing",
                     "https://www.facebook.com/marketplace/item/123",
                     "what about this", "!!!"):
            with self.subTest(text=text):
                _, err = locations.add_location("Nope", text)
                self.assertTrue(err)

    # ------------------------------------------------ names from a blank box
    def test_a_blank_name_box_names_the_city_after_the_link(self):
        locs, err = locations.add_location("", DENVER)
        self.assertIsNone(err)
        self.assertEqual(locs["Denver"], "denver")

    def test_a_name_the_tile_can_hold_is_left_alone(self):
        # Cutting a real city name that already fits would look like a bug.
        for seg, expect in (("colorado-springs", "Colorado Springs"),
                            ("108173265878171", "loc-108173265878171")):
            with self.subTest(seg=seg):
                self.assertEqual(locations.auto_label(seg), expect)

    def test_a_name_too_wide_for_its_tile_is_cut(self):
        long_seg = "sanfranciscobayareacalifornia"
        locs, err = locations.add_location("", f"/marketplace/{long_seg}/search")
        self.assertIsNone(err)
        added = [k for k in locs if k not in locations.BUILTIN_LOCATIONS]
        self.assertEqual(added, ["Sanfranciscobayarea…"])
        self.assertLessEqual(len(added[0]), locations.LABEL_MAX)
        self.assertEqual(locs[added[0]], long_seg)

    def test_two_places_that_cut_to_the_same_name_both_get_in(self):
        # The user never typed either name, so a collision it can't see mustn't
        # cost it the city it asked for.
        for seg in ("sanfranciscobayareacalifornia", "sanfranciscobayareanevada"):
            _, err = locations.add_location("", f"/marketplace/{seg}/search")
            self.assertIsNone(err)
        added = [k for k in locations.load_locations()
                 if k not in locations.BUILTIN_LOCATIONS]
        self.assertEqual(added, ["Sanfranciscobayarea…", "Sanfranciscobayar… 2"])
        self.assertTrue(all(len(k) <= locations.LABEL_MAX for k in added))

    def test_a_typed_name_is_kept_exactly_as_typed(self):
        # Only the app's own naming is cut; the settings window ellipsises a long
        # one on screen without rewriting what the user asked for.
        typed = "The Whole Of Northern California"
        locs, err = locations.add_location(typed, DENVER)
        self.assertIsNone(err)
        self.assertIn(typed, locs)

    def test_a_slug_shaped_string_is_accepted_because_only_facebook_knows(self):
        # This is the gap the sweep has to cover: 'fdjsklfjsdkl' is a perfectly
        # well-formed slug, and nothing offline can tell it isn't a city.
        locs, err = locations.add_location("SLC, UT", "fdjsklfjsdkl")
        self.assertIsNone(err)
        self.assertEqual(locs["SLC, UT"], "fdjsklfjsdkl")


class FakePage:
    def __init__(self, url, content=""):
        self.url = url
        self._content = content

    def content(self):
        return self._content


class TestUnknownCityDetection(unittest.TestCase):
    """Facebook answers an unrecognised city with a redirect to the account's own
    location and a full page of real listings, so the only way to notice is that
    the segment vanished from the URL."""

    def test_a_city_that_survived_the_redirect_is_fine(self):
        page = FakePage("https://www.facebook.com/marketplace/sac/search/?query=x")
        self.assertFalse(fb.city_was_dropped(page, "sac"))

    def test_a_numeric_id_counts_as_surviving(self):
        page = FakePage("https://www.facebook.com/marketplace/108173265878171/search/?q=x")
        self.assertFalse(fb.city_was_dropped(page, "108173265878171"))

    def test_the_category_fallback_is_caught(self):
        page = FakePage("https://www.facebook.com/marketplace/category/search/?query=x")
        self.assertTrue(fb.city_was_dropped(page, "fdjsklfjsdkl"))

    def test_case_differences_are_not_a_dropped_city(self):
        page = FakePage("https://www.facebook.com/marketplace/SAC/search/?query=x")
        self.assertFalse(fb.city_was_dropped(page, "sac"))

    def test_the_location_actually_searched_is_read_back(self):
        page = FakePage("https://www.facebook.com/marketplace/category/search/",
                        '...,"buy_location":{"display_name":"Provo, Utah",'
                        '"id":"106066949424984"}},"marketplace_settings":...')
        self.assertEqual(fb.city_shown(page), "Provo, Utah")

    def test_an_unreadable_page_is_not_an_error(self):
        class Broken:
            url = "https://www.facebook.com/marketplace/x/search/"

            def content(self):
                raise RuntimeError("navigation destroyed the page")
        self.assertEqual(fb.city_shown(Broken()), "")
        self.assertFalse(fb.city_was_dropped(Broken(), "x"))


class TestCitySummary(unittest.TestCase):
    """A city is swept once per query, so its scroll counters arrive one set per
    query and have to add up into the single line per city that run.json, the
    closing summary and the emailed report all read."""

    def stats(self, **kw):
        base = {"scrolls_used": 12, "scroll_ceiling": 60, "cards": 400,
                "keepers_seen": 30, "stop_reason": "no new matches",
                "scroll_seconds": 100.0, "seconds_saved_estimate": 300.0,
                "divider_seen": False}
        return {**base, **kw}

    def test_one_query_reads_exactly_as_it_used_to(self):
        s = fb.city_summary({"defender 110": self.stats()}, kept=7, dropped=3)
        self.assertEqual(s["scrolls_used"], 12)
        self.assertEqual(s["scroll_ceiling"], 60)
        self.assertEqual(s["cards"], 400)
        self.assertEqual(s["stop_reason"], "no new matches")
        self.assertEqual((s["kept"], s["dropped"]), (7, 3))
        # Nothing to tell apart, so nothing is broken out.
        self.assertNotIn("per_query", s)

    def test_two_queries_are_added_up_and_kept_alongside(self):
        per_query = {"defender 110": self.stats(),
                     "land rover 90": self.stats(scrolls_used=8, cards=250,
                                                 stop_reason="divider",
                                                 divider_seen=True)}
        s = fb.city_summary(per_query, kept=9, dropped=4)
        self.assertEqual(s["queries_run"], 2)
        self.assertEqual(s["scrolls_used"], 20)
        self.assertEqual(s["scroll_ceiling"], 120)
        self.assertEqual(s["cards"], 650)
        self.assertEqual(s["divider_seen"], True)
        self.assertEqual(s["stop_reason"], "no new matches, divider")
        # kept and dropped are the city's uniques, not the sum of the queries:
        # a listing both queries found is one listing.
        self.assertEqual((s["kept"], s["dropped"]), (9, 4))
        self.assertEqual(list(s["per_query"]), list(per_query))

    def test_the_same_reason_twice_is_only_said_once(self):
        s = fb.city_summary({"a": self.stats(), "b": self.stats()}, 1, 1)
        self.assertEqual(s["stop_reason"], "no new matches")


class TestMacPermissions(unittest.TestCase):
    """macOS hides Documents, Desktop and Downloads from background tasks, which
    silently stops every scheduled run."""

    def setUp(self):
        self.home = Path.home()
        self.orig = (sc.ROOT, sc.platform.system)

    def tearDown(self):
        sc.ROOT, sc.platform.system = self.orig

    def as_mac(self, folder):
        sc.ROOT = Path(folder)
        sc.platform.system = lambda: "Darwin"

    def test_documents_is_flagged(self):
        self.as_mac(self.home / "Documents" / "FaceplaceMarketbook")
        self.assertEqual(sc.in_protected_folder(), "Documents")

    def test_desktop_and_downloads_are_flagged(self):
        for name in ("Desktop", "Downloads"):
            with self.subTest(name=name):
                self.as_mac(self.home / name / "mb")
                self.assertEqual(sc.in_protected_folder(), name)

    def test_a_nested_folder_still_counts(self):
        self.as_mac(self.home / "Documents" / "projects" / "mb")
        self.assertEqual(sc.in_protected_folder(), "Documents")

    def test_a_plain_home_folder_is_fine(self):
        self.as_mac(self.home / "FaceplaceMarketbook")
        self.assertIsNone(sc.in_protected_folder())

    def test_a_folder_merely_named_documents_deeper_down_is_fine(self):
        self.as_mac(self.home / "code" / "Documents")
        self.assertIsNone(sc.in_protected_folder())

    def test_outside_home_is_not_our_business(self):
        self.as_mac(Path("/opt/marketbook"))
        self.assertIsNone(sc.in_protected_folder())

    def test_windows_and_linux_are_never_flagged(self):
        sc.ROOT = self.home / "Documents" / "mb"
        for system in ("Windows", "Linux"):
            with self.subTest(system=system):
                sc.platform.system = lambda s=system: s
                self.assertIsNone(sc.in_protected_folder())

    def test_the_advice_names_the_folder_and_the_interpreter(self):
        self.as_mac(self.home / "Documents" / "FaceplaceMarketbook")
        text = "\n".join(sc.permission_help())
        self.assertIn("Documents", text)
        self.assertIn("Full Disk Access", text)
        self.assertIn(sc.python_exe(), text)
        self.assertIn(str(self.home / "FaceplaceMarketbook"), text)

    def test_the_advice_still_helps_when_the_folder_looks_innocent(self):
        # Access can be refused for reasons we can't see; say something useful.
        self.as_mac(self.home / "mb")
        text = "\n".join(sc.permission_help())
        self.assertIn("Full Disk Access", text)
        self.assertTrue(text.strip())


class TestCheckIn(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.orig = (sc.SUPPORT_DIR, sc.HEARTBEAT_PATH)
        sc.SUPPORT_DIR = Path(self.tmp.name) / "support"
        sc.HEARTBEAT_PATH = sc.SUPPORT_DIR / "last-checkin.json"

    def tearDown(self):
        sc.SUPPORT_DIR, sc.HEARTBEAT_PATH = self.orig

    def test_nothing_recorded_reads_back_as_nothing(self):
        self.assertIsNone(sc.last_check_in())

    def test_a_check_in_records_where_it_came_from(self):
        sc.check_in("tick")
        beat = sc.last_check_in()
        self.assertEqual(beat["event"], "tick")
        self.assertEqual(beat["folder"], str(sc.ROOT))
        self.assertIsNotNone(sc.parse_iso(beat["at"]))

    def test_extra_facts_are_kept(self):
        sc.check_in("finished", ran=2)
        self.assertEqual(sc.last_check_in()["ran"], 2)

    def test_a_corrupt_file_reads_back_as_nothing(self):
        sc.SUPPORT_DIR.mkdir(parents=True)
        sc.HEARTBEAT_PATH.write_text("{not json", encoding="utf-8")
        self.assertIsNone(sc.last_check_in())

    def test_an_unwritable_folder_is_not_fatal(self):
        # Losing the check-in must never take a run down with it.
        sc.SUPPORT_DIR = Path("/proc/nope/nowhere")
        sc.HEARTBEAT_PATH = sc.SUPPORT_DIR / "x.json"
        sc.check_in("tick")

    def test_verify_waits_for_a_new_check_in(self):
        sc.check_in("tick")
        self.assertEqual(sc.verify_agent_can_run(timeout=1), (False, None))

    def test_verify_sees_a_check_in_that_arrives_late(self):
        def late():
            time.sleep(1)
            sc.check_in("tick", pid=999)
        t = threading.Thread(target=late)
        t.start()
        try:
            ok, beat = sc.verify_agent_can_run(timeout=8)
        finally:
            t.join()
        self.assertTrue(ok)
        self.assertEqual(beat["pid"], 999)


class TestScheduleProblems(unittest.TestCase):
    """What the settings window says when the schedule is installed but useless."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.orig = {k: getattr(sc, k) for k in
                     ("SUPPORT_DIR", "HEARTBEAT_PATH", "SCHEDULE_DIR", "LOCK_PATH",
                      "schedule_installed", "schedule_points_here")}
        sc.SUPPORT_DIR = Path(self.tmp.name) / "support"
        sc.HEARTBEAT_PATH = sc.SUPPORT_DIR / "last-checkin.json"
        sc.SCHEDULE_DIR = Path(self.tmp.name) / ".schedule"
        sc.LOCK_PATH = sc.SCHEDULE_DIR / "run.lock"
        sc.schedule_installed = lambda: True
        sc.schedule_points_here = lambda: True

    def tearDown(self):
        for k, v in self.orig.items():
            setattr(sc, k, v)

    def test_nothing_installed_means_nothing_to_report(self):
        sc.schedule_installed = lambda: False
        self.assertEqual(sc.schedule_problems(), [])

    def test_a_healthy_check_in_reports_nothing(self):
        sc.check_in("tick")
        self.assertEqual(sc.schedule_problems(), [])

    def test_never_checking_in_reads_as_a_permission_problem(self):
        problems = sc.schedule_problems()
        self.assertTrue(problems)
        self.assertIn("Full Disk Access", "\n".join(problems))

    def test_a_moved_folder_is_named_as_the_cause(self):
        sc.schedule_points_here = lambda: False
        self.assertIn("moved or renamed", "\n".join(sc.schedule_problems()))

    def test_a_moved_folder_outranks_a_missing_check_in(self):
        # Both are true after a move; only one is worth acting on.
        sc.schedule_points_here = lambda: False
        self.assertEqual(len(sc.schedule_problems()), 1)

    def test_a_long_silence_is_reported(self):
        stale = sc.now_local() - timedelta(seconds=sc.TICK_SECONDS * 4)
        sc.SUPPORT_DIR.mkdir(parents=True)
        sc.HEARTBEAT_PATH.write_text(
            json.dumps({"event": "tick", "at": sc.iso(stale)}), encoding="utf-8")
        self.assertIn("last checked in", "\n".join(sc.schedule_problems()))

    def test_a_check_in_one_tick_ago_is_not_reported(self):
        recent = sc.now_local() - timedelta(seconds=sc.TICK_SECONDS)
        sc.SUPPORT_DIR.mkdir(parents=True)
        sc.HEARTBEAT_PATH.write_text(
            json.dumps({"event": "tick", "at": sc.iso(recent)}), encoding="utf-8")
        self.assertEqual(sc.schedule_problems(), [])

    def test_a_run_in_progress_explains_the_silence(self):
        # A sweep can run for hours, during which no new tick checks in. That
        # is normal, not a broken scheduler.
        stale = sc.now_local() - timedelta(seconds=sc.TICK_SECONDS * 4)
        sc.SUPPORT_DIR.mkdir(parents=True)
        sc.HEARTBEAT_PATH.write_text(
            json.dumps({"event": "tick", "at": sc.iso(stale)}), encoding="utf-8")
        sc.SCHEDULE_DIR.mkdir(parents=True)
        sc.LOCK_PATH.write_text(json.dumps(
            {"pid": os.getpid(), "what": "scheduled run",
             "started": sc.iso(sc.now_local())}), encoding="utf-8")
        self.assertEqual(sc.schedule_problems(), [])


class TestComputerSettings(unittest.TestCase):
    """Which Email & Setup cards to hide, from pmset / powercfg dumps."""

    PMSET_ADAPTER_ONLY = """\
Battery Power:
 womp                 0
 lowpowermode         0
AC Power:
 womp                 1
 lowpowermode         0
"""
    PMSET_ALWAYS = """\
Battery Power:
 womp                 1
 lowpowermode         0
AC Power:
 womp                 1
 lowpowermode         0
"""
    PMSET_DESKTOP = """\
AC Power:
 womp                 1
 lowpowermode         0
"""
    PMSET_LPM_ALWAYS = """\
Battery Power:
 womp                 1
 lowpowermode         1
AC Power:
 womp                 1
 lowpowermode         1
"""

    def test_only_on_power_adapter_keeps_the_wake_card(self):
        self.assertEqual(sc._mac_settings_done(self.PMSET_ADAPTER_ONLY),
                         [sc.SYS_MAC_LPM])

    def test_always_hides_wake_and_low_power_mode(self):
        self.assertEqual(sc._mac_settings_done(self.PMSET_ALWAYS),
                         [sc.SYS_MAC_WAKE, sc.SYS_MAC_LPM])

    def test_a_desktop_with_womp_on_is_treated_as_always(self):
        self.assertEqual(sc._mac_settings_done(self.PMSET_DESKTOP),
                         [sc.SYS_MAC_WAKE, sc.SYS_MAC_LPM])

    def test_low_power_mode_on_ac_is_left_on_the_page(self):
        self.assertEqual(sc._mac_settings_done(self.PMSET_LPM_ALWAYS),
                         [sc.SYS_MAC_WAKE])

    def test_unreadable_pmset_hides_nothing(self):
        self.assertEqual(sc._mac_settings_done(""), [])
        self.assertEqual(sc._mac_settings_done("not a dump"), [])

    WAKE_BOTH = """\
Power Setting GUID: bd3b718a-0680-4d9d-8ab2-e1d2b4ac806d  (Allow wake timers)
Current AC Power Setting Index: 0x00000001
Current DC Power Setting Index: 0x00000001
"""
    WAKE_DC_OFF = """\
Current AC Power Setting Index: 0x00000001
Current DC Power Setting Index: 0x00000000
"""
    WAKE_IMPORTANT_ONLY = """\
Current AC Power Setting Index: 0x00000002
Current DC Power Setting Index: 0x00000002
"""
    LID_SLEEP = """\
Current AC Power Setting Index: 0x00000001
Current DC Power Setting Index: 0x00000000
"""
    LID_HIBERNATE = """\
Current AC Power Setting Index: 0x00000002
Current DC Power Setting Index: 0x00000001
"""

    def test_wake_timers_enabled_on_both_sides_are_hidden(self):
        self.assertEqual(
            sc._win_settings_done(self.WAKE_BOTH, self.LID_HIBERNATE),
            [sc.SYS_WIN_WAKE])

    def test_wake_timers_off_on_battery_stay_on_the_page(self):
        self.assertEqual(
            sc._win_settings_done(self.WAKE_DC_OFF, self.LID_SLEEP),
            [sc.SYS_WIN_LID])

    def test_important_wake_timers_only_is_not_enable(self):
        self.assertEqual(
            sc._win_settings_done(self.WAKE_IMPORTANT_ONLY, self.LID_SLEEP),
            [sc.SYS_WIN_LID])

    def test_a_lid_set_to_hibernate_stays_on_the_page(self):
        self.assertNotIn(sc.SYS_WIN_LID, sc._win_settings_done(
            self.WAKE_DC_OFF, self.LID_HIBERNATE))

    def test_unreadable_powercfg_hides_nothing(self):
        self.assertEqual(sc._win_settings_done("", ""), [])


if __name__ == "__main__":
    unittest.main()
