#!/usr/bin/env python3
"""
Offline tests for the Past searches tab.

    python3 -m unittest discover tests

Nothing here opens a browser: the one call that would is replaced, and every
folder these read is built in a temporary directory first.

Nothing here starts the localhost gallery server either, and — just as
important — nothing here asks whether one is already running. It would be, on
the machine of anyone who has the app open, and then these tests would pass or
fail depending on that rather than on the code. Which of the two answers
open_run gets is set per test instead.
"""
import csv
import json
import shutil
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import past_runs as pr
import scheduling as sc


class Redirected(unittest.TestCase):
    """Points the runs folder at a throwaway one, and keeps the browser shut."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.runs = Path(self.tmp.name) / "runs"
        self.runs.mkdir()
        self._saved = pr.RUNS_DIR
        pr.RUNS_DIR = self.runs
        self.opened = []
        self._real_open = pr.webbrowser.open
        pr.webbrowser.open = lambda url, **kw: self.opened.append(url) or True
        # Answering without going near a socket. The default is the usual case:
        # the app is open, so the server it starts is up.
        self.server_up = True
        self._real_ensure = sc.ensure_gallery_server
        sc.ensure_gallery_server = lambda *a, **kw: self.server_up
        # And who holds the port, which decides the wording when it's down.
        # Stubbed for the same reason: left real, these read whatever happens to
        # be listening on this machine and the answer changes by the day.
        self.on_port = None
        self._real_here = sc.gallery_server_here
        sc.gallery_server_here = lambda *a, **kw: self.on_port
        self.addCleanup(self._restore)

    def _restore(self):
        pr.RUNS_DIR = self._saved
        pr.webbrowser.open = self._real_open
        sc.ensure_gallery_server = self._real_ensure
        sc.gallery_server_here = self._real_here
        self.tmp.cleanup()

    # ------------------------------------------------------------- fixtures
    def make_run(self, name, manifest=None, rows=2, gallery=True,
                 scheduled=False, files=()):
        folder = (self.runs / "saved" / name) if scheduled else (self.runs / name)
        folder.mkdir(parents=True)
        with open(folder / "results.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["item_id", "title"])
            w.writeheader()
            for i in range(rows):
                w.writerow({"item_id": f"{name}-{i}", "title": f"Listing {i}"})
        if manifest is not None:
            (folder / "run.json").write_text(json.dumps(manifest),
                                             encoding="utf-8")
        if gallery:
            (folder / "gallery.html").write_text("<html>", encoding="utf-8")
        for extra in files:
            (folder / extra).write_text("x", encoding="utf-8")
        return folder

    def manifest(self, query="defender 110", listings=12, **kw):
        return {"query": query, "unique_listings": listings,
                "finished": "2026-08-09T23:14:26+00:00",
                "duration_seconds": 128.5,
                "locations": {"Medford, OR": "1", "Denver, CO": "2"}, **kw}

    def only(self):
        runs = pr.list_runs()["runs"]
        self.assertEqual(len(runs), 1, runs)
        return runs[0]


class Listing(Redirected):
    def test_an_empty_runs_folder_lists_nothing(self):
        self.assertEqual(pr.list_runs(), {"runs": []})

    def test_a_missing_runs_folder_is_not_an_error(self):
        pr.RUNS_DIR = self.runs / "not-there"
        self.assertEqual(pr.list_runs(), {"runs": []})

    def test_the_manifest_is_where_the_numbers_come_from(self):
        self.make_run("defender_110_08-09-2026", self.manifest())
        card = self.only()
        self.assertEqual(card["name"], "defender 110")
        self.assertEqual(card["listings"], 12)
        self.assertEqual(card["cities"], 2)
        self.assertEqual(card["duration_text"], "2m 8s")
        self.assertFalse(card["scheduled"])
        self.assertEqual(card["id"], "defender_110_08-09-2026")

    def test_a_run_with_no_manifest_still_gets_a_card(self):
        # Interrupted before run.json, or written by a version that predates it.
        # A run nobody can see is a run they'll pay to do again.
        self.make_run("rover_08-01-2026", manifest=None, rows=3)
        card = self.only()
        self.assertEqual(card["listings"], 3)
        # The date is already said in words, so the folder's copy of it goes.
        self.assertEqual(card["name"], "rover")
        self.assertIsNone(card["duration_text"])

    def test_a_folder_holding_nothing_of_ours_is_not_a_run(self):
        (self.runs / "notes").mkdir()
        (self.runs / "notes" / "thoughts.txt").write_text("hi", encoding="utf-8")
        (self.runs / "loose.txt").write_text("hi", encoding="utf-8")
        self.assertEqual(pr.list_runs()["runs"], [])

    def test_a_saved_search_folder_is_marked_as_scheduled(self):
        self.make_run("defender_130", self.manifest(query="defender 130"),
                      scheduled=True)
        card = self.only()
        self.assertTrue(card["scheduled"])
        self.assertEqual(card["id"], "saved/defender_130")

    def test_a_scheduled_run_says_how_many_were_new(self):
        self.make_run("defender_130",
                      self.manifest(saved_search={"new_listings": 4}),
                      scheduled=True)
        self.assertEqual(self.only()["new_listings"], 4)

    def test_the_kept_history_of_a_scheduled_search_is_counted(self):
        folder = self.make_run("defender_130", self.manifest(), scheduled=True)
        hist = folder / "history"
        hist.mkdir()
        for n in (1, 2, 3):
            (hist / f"run-{n}.json").write_text("{}", encoding="utf-8")
            (hist / f"report-{n}.html").write_text("x", encoding="utf-8")
        self.assertEqual(self.only()["earlier_runs"], 3)

    def test_the_newest_run_comes_first(self):
        self.make_run("old_08-01-2026", self.manifest(
            query="old", finished="2026-08-01T10:00:00"))
        self.make_run("new_08-09-2026", self.manifest(
            query="new", finished="2026-08-09T10:00:00"))
        self.make_run("middle_08-05-2026", self.manifest(
            query="middle", finished="2026-08-05T10:00:00"))
        self.assertEqual([r["name"] for r in pr.list_runs()["runs"]],
                         ["new", "middle", "old"])

    def test_a_run_with_no_recorded_time_falls_back_to_the_files(self):
        self.make_run("rover_08-01-2026", manifest=None)
        card = self.only()
        self.assertTrue(card["when"])
        self.assertNotEqual(card["when_text"], "never")

    def test_manual_and_scheduled_runs_share_the_one_list(self):
        self.make_run("defender_110_08-09-2026", self.manifest(
            query="by hand", finished="2026-08-09T10:00:00"))
        self.make_run("nightly", self.manifest(
            query="on a schedule", finished="2026-08-08T10:00:00"),
            scheduled=True)
        self.assertEqual([r["name"] for r in pr.list_runs()["runs"]],
                         ["by hand", "on a schedule"])


class Opening(Redirected):
    def served(self, folder, name="gallery.html"):
        return sc.gallery_url((folder / name).resolve())

    def test_clicking_a_run_opens_its_gallery(self):
        folder = self.make_run("defender_110_08-09-2026", self.manifest())
        res = pr.open_run("defender_110_08-09-2026")
        self.assertNotIn("error", res)
        self.assertEqual(self.opened, [self.served(folder)])

    def test_the_gallery_is_served_rather_than_handed_over_as_a_file(self):
        # Same page either way, but only a served one can save a star: the page
        # has somewhere to send it, and the file on disk gets rewritten so the
        # mark is in the copy you'd email.
        folder = self.make_run("defender_110_08-09-2026", self.manifest())
        pr.open_run("defender_110_08-09-2026")
        self.assertTrue(self.opened[0].startswith("http://127.0.0.1:"))
        self.assertIn("gallery.html", self.opened[0])
        self.assertTrue((folder / "gallery.html").exists())

    def test_without_a_server_the_file_itself_is_opened_instead(self):
        # Better a read-only gallery than no gallery.
        folder = self.make_run("defender_110_08-09-2026", self.manifest())
        self.server_up = False
        res = pr.open_run("defender_110_08-09-2026")
        self.assertNotIn("error", res)
        self.assertEqual(self.opened,
                         [(folder / "gallery.html").resolve().as_uri()])

    def test_a_read_only_gallery_says_so_in_the_window(self):
        # It opened, so nothing looks wrong. The stars quietly not saving is
        # the kind of thing someone finds out days later.
        self.make_run("defender_110_08-09-2026", self.manifest())
        self.server_up = False
        note = pr.open_run("defender_110_08-09-2026")["note"]
        self.assertIn("isn't able to save your changes", note)

    def test_the_read_only_note_says_what_to_do_about_it(self):
        # Whoever reads this is somewhere they didn't mean to be, and the two
        # causes they can't fix are indistinguishable from the one they can —
        # so it opens with the retry that covers both of those.
        self.make_run("defender_110_08-09-2026", self.manifest())
        self.server_up = False
        note = pr.open_run("defender_110_08-09-2026")["note"]
        self.assertIn("1. Click Open the gallery again", note)
        if sc.os_name() in sc.FIREWALL_STEPS:
            self.assertIn("firewall", note.lower())
            self.assertIn("3.", note)

    def test_a_second_copy_of_the_app_is_named_rather_than_guessed_at(self):
        # The one failure that can be identified for certain. Sending someone
        # to their firewall settings over this costs hours: it opens fine, it
        # just quietly can't save, and nothing in the firewall is wrong.
        self.make_run("defender_110_08-09-2026", self.manifest())
        self.server_up = False
        self.on_port = {"app": sc.APP_MARK, "runs": "/elsewhere/runs"}
        note = pr.open_run("defender_110_08-09-2026")["note"]
        self.assertIn("Another copy of Faceplace Marketbook", note)
        self.assertIn("/elsewhere/runs", note)
        self.assertNotIn("firewall", note.lower())

    def test_a_gallery_that_opened_normally_says_nothing(self):
        self.make_run("defender_110_08-09-2026", self.manifest())
        self.assertEqual(pr.open_run("defender_110_08-09-2026"), {})

    def test_the_self_contained_gallery_is_preferred(self):
        # The lightweight one only works beside its thumbnails folder.
        folder = self.make_run("defender_110_08-09-2026", self.manifest(),
                               files=["lightweight_gallery.html"])
        pr.open_run("defender_110_08-09-2026")
        self.assertEqual(self.opened, [self.served(folder)])

    def test_the_lightweight_one_is_opened_when_it_is_all_there_is(self):
        folder = self.make_run("defender_110_08-09-2026", self.manifest(),
                               gallery=False, files=["lightweight_gallery.html"])
        pr.open_run("defender_110_08-09-2026")
        self.assertEqual(self.opened,
                         [self.served(folder, "lightweight_gallery.html")])

    def test_a_run_that_never_built_one_gets_a_gallery_built_now(self):
        folder = self.make_run("no_gallery_08-09-2026", self.manifest(),
                               gallery=False)
        res = pr.open_run("no_gallery_08-09-2026")
        self.assertNotIn("error", res)
        self.assertTrue((folder / "gallery.html").exists())
        self.assertEqual(len(self.opened), 1)

    def test_a_folder_that_has_gone_says_so_rather_than_failing(self):
        res = pr.open_run("defender_110_08-09-2026")
        self.assertIn("error", res)
        self.assertEqual(self.opened, [])

    def test_an_id_that_climbs_out_of_the_runs_folder_is_refused(self):
        # The id makes the round trip through the page, so it arrives back as
        # whatever the page felt like sending.
        for escape in ("..", "../..", "saved/../..", "/etc"):
            with self.subTest(escape=escape):
                self.assertIsNone(pr.folder_for(escape))
                self.assertIn("error", pr.open_run(escape))
        self.assertEqual(self.opened, [])

    def test_a_browser_that_will_not_open_names_the_file_instead(self):
        self.make_run("defender_110_08-09-2026", self.manifest())

        def refuse(url, **kw):
            raise RuntimeError("no browser here")
        pr.webbrowser.open = refuse
        res = pr.open_run("defender_110_08-09-2026")
        self.assertIn("gallery.html", res["error"])


class Deleting(Redirected):
    def test_deleting_a_run_takes_the_whole_folder(self):
        folder = self.make_run("defender_110_08-09-2026", self.manifest())
        (folder / "thumbnails").mkdir()
        (folder / "thumbnails" / "one.jpg").write_bytes(b"x")
        res = pr.delete_run("defender_110_08-09-2026")
        self.assertNotIn("error", res)
        self.assertFalse(folder.exists())
        self.assertEqual(res["runs"], [])

    def test_what_is_left_comes_back_with_the_answer(self):
        # The window redraws the list from this rather than asking again.
        self.make_run("defender_110_08-09-2026", self.manifest())
        self.make_run("bronco_08-10-2026", self.manifest(query="bronco"))
        res = pr.delete_run("defender_110_08-09-2026")
        self.assertEqual([r["id"] for r in res["runs"]], ["bronco_08-10-2026"])

    def test_a_saved_searchs_folder_can_go_like_any_other(self):
        folder = self.make_run("defender-110", self.manifest(), scheduled=True)
        (folder / "history").mkdir()
        res = pr.delete_run("saved/defender-110")
        self.assertNotIn("error", res)
        self.assertFalse(folder.exists())
        # Only that search's folder, not the shelf they all sit on.
        self.assertTrue((self.runs / "saved").is_dir())

    def test_the_whole_saved_folder_cannot_be_deleted_at_once(self):
        # It's inside runs/ and it is a directory, but it was never a card.
        self.make_run("defender-110", self.manifest(), scheduled=True)
        self.assertIn("error", pr.delete_run("saved"))
        self.assertTrue((self.runs / "saved" / "defender-110").is_dir())

    def test_a_folder_that_has_already_gone_says_so(self):
        res = pr.delete_run("defender_110_08-09-2026")
        self.assertIn("error", res)

    def test_an_id_that_climbs_out_of_the_runs_folder_is_refused(self):
        outside = Path(self.tmp.name) / "keep-me"
        outside.mkdir()
        for escape in ("..", "../keep-me", "saved/../..", "/etc"):
            with self.subTest(escape=escape):
                self.assertIn("error", pr.delete_run(escape))
        self.assertTrue(outside.is_dir())
        self.assertTrue(self.runs.is_dir())

    def test_a_folder_that_will_not_go_says_why(self):
        self.make_run("defender_110_08-09-2026", self.manifest())

        def refuse(path):
            raise OSError("file in use")
        # Put back before this test's temporary folder is cleaned up, which
        # goes through the same function.
        self.addCleanup(setattr, shutil, "rmtree", shutil.rmtree)
        shutil.rmtree = refuse
        self.assertIn("file in use",
                      pr.delete_run("defender_110_08-09-2026")["error"])


class Hooks(Redirected):
    def test_the_window_gets_the_three_hooks_it_looks_for(self):
        self.assertEqual(set(pr.ui_hooks()),
                         {"list_runs", "open_run", "delete_run"})


if __name__ == "__main__":
    unittest.main()
