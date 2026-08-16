#!/usr/bin/env python3
"""
Stars and hides: where they're kept, what's allowed into them, and the server
the gallery page saves them through.

    python3 -m unittest tests.test_marks

Offline and browser-free. tests/test_gallery_ui.py drives the page itself,
including the round trip through a real server from a real file:// gallery;
this file is about everything underneath that.

The marks arrive from a web page, so most of what's here is about the payload
being untrusted: ids that aren't strings, ids long enough to be an attack on the
filesystem, run ids trying to name a folder outside runs/, and bodies big enough
to be worth refusing before they're read.
"""
import json
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import build_gallery  # noqa: E402
import marks  # noqa: E402
import scheduling as sc  # noqa: E402
import storage  # noqa: E402

LISTINGS = [
    {"item_id": "111", "title": "1972 Bronco", "price": "$8,000"},
    {"item_id": "222", "title": "1965 Land Cruiser", "price": "$12,000"},
    {"item_id": "333", "title": "1994 Defender 110", "price": "$40,000"},
]


class TestClean(unittest.TestCase):
    """What's allowed to become a mark."""

    def test_two_lists_of_ids_come_back_as_they_went_in(self):
        self.assertEqual(marks.clean({"starred": ["1"], "hidden": ["2"]}),
                         {"starred": ["1"], "hidden": ["2"]})

    def test_anything_that_is_not_a_dict_is_no_marks_at_all(self):
        for junk in (None, [], "starred", 7):
            self.assertEqual(marks.clean(junk), marks.empty())

    def test_ids_that_are_not_strings_are_dropped(self):
        self.assertEqual(
            marks.clean({"starred": [1, None, {"a": 1}, ["2"], "3"]}),
            {"starred": ["3"], "hidden": []})

    def test_an_absurdly_long_id_is_dropped(self):
        long = "9" * (marks.MAX_ID_LEN + 1)
        self.assertEqual(marks.clean({"starred": [long, "ok"]})["starred"],
                         ["ok"])

    def test_an_empty_id_is_dropped(self):
        self.assertEqual(marks.clean({"hidden": ["", "ok"]})["hidden"], ["ok"])

    def test_duplicates_within_a_list_are_collapsed(self):
        self.assertEqual(marks.clean({"starred": ["a", "a", "b"]})["starred"],
                         ["a", "b"])

    def test_a_listing_cannot_be_starred_and_hidden_at_once(self):
        # They're opposite verdicts. The page enforces it too, but the page is
        # not what this has to be true of.
        self.assertEqual(marks.clean({"starred": ["a"], "hidden": ["a", "b"]}),
                         {"starred": ["a"], "hidden": ["b"]})

    def test_far_too_many_ids_are_cut_off(self):
        many = [str(n) for n in range(marks.MAX_IDS + 500)]
        self.assertEqual(len(marks.clean({"starred": many})["starred"]),
                         marks.MAX_IDS)

    def test_a_missing_list_is_an_empty_one(self):
        self.assertEqual(marks.clean({"starred": ["a"]})["hidden"], [])


class TestFile(unittest.TestCase):
    """marks.json, in the run's own folder."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)

    def test_a_folder_with_no_file_in_it_has_no_marks(self):
        self.assertEqual(marks.read(self.folder), marks.empty())

    def test_what_was_written_is_what_comes_back(self):
        marks.write(self.folder, {"starred": ["1"], "hidden": ["2"]})
        self.assertEqual(marks.read(self.folder),
                         {"starred": ["1"], "hidden": ["2"]})

    def test_a_hand_mangled_file_reads_as_no_marks_rather_than_raising(self):
        # It's a file on disk in a folder people open. A gallery that won't
        # build because someone opened marks.json in a text editor would be a
        # worse failure than one that opens unmarked.
        (self.folder / marks.MARKS_NAME).write_text("{not json", encoding="utf-8")
        self.assertEqual(marks.read(self.folder), marks.empty())

    def test_junk_inside_a_readable_file_is_still_filtered(self):
        (self.folder / marks.MARKS_NAME).write_text(
            json.dumps({"starred": [1, "ok"], "hidden": "nope"}), encoding="utf-8")
        self.assertEqual(marks.read(self.folder),
                         {"starred": ["ok"], "hidden": []})

    def test_saving_says_when_it_happened(self):
        marks.write(self.folder, {"starred": ["1"]})
        body = json.loads((self.folder / marks.MARKS_NAME).read_text())
        self.assertIn("updated", body)

    def test_no_temporary_file_is_left_behind(self):
        marks.write(self.folder, {"starred": ["1"]})
        self.assertEqual([p.name for p in self.folder.iterdir()],
                         [marks.MARKS_NAME])


class TestRunFolder(unittest.TestCase):
    """A run id off a web page back to a folder. Same containment rule the
    gallery server applies to files, for the same reason."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runs = Path(self.tmp.name) / "runs"
        (self.runs / "saved" / "d110").mkdir(parents=True)

    def test_a_folder_under_runs_resolves(self):
        self.assertEqual(marks.run_folder("saved/d110", self.runs),
                         (self.runs / "saved" / "d110").resolve())

    def test_dot_dot_cannot_walk_out_of_runs(self):
        self.assertIsNone(marks.run_folder("../../etc", self.runs))

    def test_an_absolute_path_elsewhere_is_refused(self):
        self.assertIsNone(marks.run_folder(str(Path(self.tmp.name)), self.runs))

    def test_runs_itself_is_refused(self):
        self.assertIsNone(marks.run_folder("", self.runs))
        self.assertIsNone(marks.run_folder(".", self.runs))

    def test_a_folder_that_does_not_exist_is_refused(self):
        self.assertIsNone(marks.run_folder("saved/gone", self.runs))

    def test_a_file_is_not_a_folder(self):
        (self.runs / "saved" / "d110" / "gallery.html").write_text("x")
        self.assertIsNone(
            marks.run_folder("saved/d110/gallery.html", self.runs))


class TestStamping(unittest.TestCase):
    """Rewriting the marks inside a gallery that's already been built."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)

    def page(self, block='{"starred": [], "hidden": []}'):
        return ('<html><script id="data" type="application/json">[{"a": 1}]'
                '</script>\n<script id="marks" type="application/json">'
                + block + '</script>\n<p>after</p></html>')

    def test_the_marks_block_is_replaced(self):
        out = marks.stamp(self.page(), {"starred": ["7"], "hidden": []})
        self.assertIn('<script id="marks" type="application/json">'
                      '{"starred": ["7"], "hidden": []}</script>', out)

    def test_nothing_else_in_the_file_is_touched(self):
        out = marks.stamp(self.page(), {"starred": ["7"]})
        self.assertIn('<script id="data" type="application/json">[{"a": 1}]'
                      '</script>', out)
        self.assertIn("<p>after</p>", out)

    def test_a_backslash_in_an_id_is_not_read_as_a_group_reference(self):
        # re.sub would treat \1 in the replacement as the first group. The id
        # can't really contain one, but the bug it would cause is silent.
        out = marks.stamp(self.page(), {"starred": ["a\\1b"]})
        self.assertIn("a\\\\1b", out)

    def test_a_page_without_the_block_is_returned_unchanged(self):
        self.assertEqual(marks.stamp("<html>nothing</html>", {"starred": ["1"]}),
                         "<html>nothing</html>")

    def test_both_galleries_in_a_folder_are_brought_up_to_date(self):
        for name in marks.GALLERY_NAMES:
            (self.folder / name).write_text(self.page(), encoding="utf-8")
        changed = marks.restamp(self.folder, {"starred": ["7"]})
        self.assertEqual(sorted(p.name for p in changed),
                         sorted(marks.GALLERY_NAMES))
        for name in marks.GALLERY_NAMES:
            self.assertIn('"7"', (self.folder / name).read_text())

    def test_a_folder_with_only_one_gallery_in_it_is_fine(self):
        (self.folder / "gallery.html").write_text(self.page(), encoding="utf-8")
        self.assertEqual([p.name for p in
                          marks.restamp(self.folder, {"starred": ["7"]})],
                         ["gallery.html"])

    def test_a_gallery_already_saying_this_is_not_rewritten(self):
        # Sixty megabytes of inlined photos is not worth writing again to say
        # what the file already says.
        page = marks.stamp(self.page(), {"starred": ["7"]})
        (self.folder / "gallery.html").write_text(page, encoding="utf-8")
        self.assertEqual(marks.restamp(self.folder, {"starred": ["7"]}), [])


class TestBuild(unittest.TestCase):
    """What build_gallery puts in the page."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runs = Path(self.tmp.name) / "runs"
        self.folder = self.runs / "defender_08-15-2026"
        self.folder.mkdir(parents=True)
        self.addCleanup(setattr, build_gallery, "RUNS_DIR",
                        build_gallery.RUNS_DIR)
        build_gallery.RUNS_DIR = self.runs
        self.csv = self.folder / "results.csv"
        storage.write_csv(LISTINGS, self.csv)

    def build(self, **kw):
        kw.setdefault("quiet", True)
        kw.setdefault("images", False)
        out = Path(build_gallery.build(self.csv, **kw))
        return out.read_text(encoding="utf-8")

    def block(self, html, which="marks"):
        start = html.index(f'<script id="{which}" type="application/json">')
        start = html.index(">", start) + 1
        return json.loads(html[start:html.index("</script>", start)])

    def test_a_gallery_knows_which_run_folder_it_is_in(self):
        self.assertEqual(self.block(self.build(), "run"), "defender_08-15-2026")

    def test_a_scheduled_searchs_folder_keeps_its_saved_prefix(self):
        folder = self.runs / "saved" / "d110"
        folder.mkdir(parents=True)
        csv_path = folder / "results.csv"
        storage.write_csv(LISTINGS, csv_path)
        html = Path(build_gallery.build(csv_path, quiet=True, images=False)
                    ).read_text(encoding="utf-8")
        self.assertEqual(self.block(html, "run"), "saved/d110")

    def test_a_gallery_built_outside_runs_has_no_run_id(self):
        out = Path(self.tmp.name) / "loose.html"
        html = Path(build_gallery.build(self.csv, out, quiet=True, images=False)
                    ).read_text(encoding="utf-8")
        self.assertIsNone(self.block(html, "run"))

    def test_an_emailed_gallery_has_no_run_id(self):
        # It opens on somebody else's computer, where that folder is either
        # missing or, worse, someone else's run of the same name.
        self.assertIsNone(self.block(self.build(editable=False), "run"))

    def test_an_emailed_gallery_still_carries_the_marks(self):
        # Sending someone your stars is the whole point of it.
        marks.write(self.folder, {"starred": ["222"]})
        self.assertEqual(self.block(self.build(editable=False))["starred"],
                         ["222"])

    def test_the_marks_in_the_folder_are_baked_into_the_page(self):
        marks.write(self.folder, {"starred": ["111"], "hidden": ["333"]})
        self.assertEqual(self.block(self.build()),
                         {"starred": ["111"], "hidden": ["333"]})

    def test_a_run_with_no_marks_yet_builds_an_empty_block(self):
        self.assertEqual(self.block(self.build()), marks.empty())

    def test_the_page_is_told_where_to_save(self):
        self.assertIn(f'"http://{marks.HOST}:{marks.PORT}{marks.ENDPOINT}"',
                      self.build())

    def test_a_listing_that_mentions_a_token_cannot_forge_one(self):
        # Descriptions are arbitrary text off Facebook. Substituting the
        # listings last is what keeps one of them from being read as a token.
        storage.write_csv(
            [{**LISTINGS[0], "description": "__MARKS__ __RUN__ __HELPER__"}],
            self.csv)
        html = self.build()
        self.assertEqual(self.block(html, "run"), "defender_08-15-2026")
        self.assertIn("__MARKS__ __RUN__ __HELPER__", html)


class TestServer(unittest.TestCase):
    """The marks endpoint on the localhost gallery server."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runs = Path(self.tmp.name) / "runs"
        self.folder = self.runs / "saved" / "d110"
        self.folder.mkdir(parents=True)
        self.addCleanup(setattr, sc, "RUNS_DIR", sc.RUNS_DIR)
        sc.RUNS_DIR = self.runs
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), sc._GalleryHandler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def url(self, run="saved/d110"):
        return f"{self.base}{marks.ENDPOINT}?run={run}"

    def get(self, run="saved/d110"):
        with urlopen(self.url(run), timeout=3) as r:
            return json.loads(r.read())

    def post(self, payload, run="saved/d110", raw=None):
        body = raw if raw is not None else json.dumps(payload).encode("utf-8")
        req = Request(self.url(run), data=body, method="POST",
                      headers={"Content-Type": "text/plain", "Origin": "null"})
        with urlopen(req, timeout=3) as r:
            return json.loads(r.read())

    def test_a_run_with_no_marks_answers_with_none(self):
        self.assertEqual(self.get(), marks.empty())

    def test_posted_marks_come_back_on_the_next_read(self):
        self.post({"starred": ["111"], "hidden": ["222"]})
        self.assertEqual(self.get(), {"starred": ["111"], "hidden": ["222"]})

    def test_posting_writes_the_file_in_the_run_folder(self):
        self.post({"starred": ["111"]})
        self.assertTrue((self.folder / marks.MARKS_NAME).exists())

    def test_posting_rewrites_the_galleries_on_disk(self):
        # The point of the whole exercise: the file you'd email is current
        # without anyone having to rebuild it.
        page = ('<html><script id="marks" type="application/json">'
                '{"starred": [], "hidden": []}</script></html>')
        for name in marks.GALLERY_NAMES:
            (self.folder / name).write_text(page, encoding="utf-8")
        self.post({"starred": ["111"]})
        for name in marks.GALLERY_NAMES:
            self.assertIn('"111"', (self.folder / name).read_text())

    def test_the_reply_is_readable_from_a_file_page(self):
        # A gallery opened from Finder has the origin "null", and without this
        # header the browser hides the answer from it.
        req = Request(self.url(), headers={"Origin": "null"})
        with urlopen(req, timeout=3) as r:
            self.assertEqual(r.headers["Access-Control-Allow-Origin"], "null")

    def test_a_preflight_is_answered(self):
        req = Request(self.url(), method="OPTIONS",
                      headers={"Origin": "null"})
        with urlopen(req, timeout=3) as r:
            self.assertEqual(r.status, 204)
            self.assertIn("POST", r.headers["Access-Control-Allow-Methods"])

    def test_junk_in_the_payload_is_filtered_before_it_is_stored(self):
        self.assertEqual(self.post({"starred": [1, "ok"], "hidden": 5}),
                         {"starred": ["ok"], "hidden": []})

    def test_a_body_that_is_not_json_is_refused(self):
        with self.assertRaises(HTTPError) as raised:
            self.post(None, raw=b"{not json")
        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()

    def test_an_empty_body_is_refused(self):
        with self.assertRaises(HTTPError) as raised:
            self.post(None, raw=b"")
        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()

    def test_an_enormous_body_is_refused_without_being_read(self):
        with self.assertRaises(HTTPError) as raised:
            self.post(None, raw=b"x" * (sc.MARKS_MAX_BYTES + 1))
        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()

    def test_a_run_outside_runs_is_refused(self):
        for bad in ("../../etc", "nope", ""):
            with self.assertRaises(HTTPError) as raised:
                self.get(bad)
            self.assertEqual(raised.exception.code, 404)
            raised.exception.close()

    def test_posting_to_a_run_outside_runs_is_refused(self):
        with self.assertRaises(HTTPError) as raised:
            self.post({"starred": ["1"]}, run="../../etc")
        self.assertEqual(raised.exception.code, 404)
        raised.exception.close()

    def test_posting_anywhere_but_the_marks_path_is_refused(self):
        req = Request(f"{self.base}/whatever", data=b"{}", method="POST")
        with self.assertRaises(HTTPError) as raised:
            urlopen(req, timeout=3)
        self.assertEqual(raised.exception.code, 404)
        raised.exception.close()

    def test_the_server_still_serves_galleries(self):
        gallery = self.folder / "gallery.html"
        gallery.write_text("<html>hello</html>", encoding="utf-8")
        with urlopen(f"{self.base}{gallery.resolve().as_posix()}",
                     timeout=3) as r:
            self.assertEqual(r.read(), b"<html>hello</html>")


if __name__ == "__main__":
    unittest.main()
