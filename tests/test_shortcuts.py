#!/usr/bin/env python3
"""
Offline tests for the desktop, Dock and Start menu shortcuts.

    python3 -m unittest discover tests

Nothing here draws an icon or puts one anywhere: rendering needs Chromium, and a
test suite that leaves things on the desktop of whoever ran it would be a rude
one. What's covered is everything around that — whether to raise the subject at
all, how each system is asked, and the two file formats written by hand.
"""
import json
import plistlib
import struct
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import make_desktop_icon as mk


class Redirected(unittest.TestCase):
    """Keeps the record of what's been made in a throwaway folder."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patch = mock.patch.object(mk, "STATE_PATH", self.root / ".shortcuts.json")
        patch.start()
        self.addCleanup(patch.stop)

    def on(self, system, taken=()):
        """Pretends to be a Mac or a Windows machine with `taken` already done.

        platform.system is the real module here, shared with everything else in
        the process, so each patch has to be undone by the end of the test that
        made it — hence starting and registering the same object, not two.
        """
        for patch in (mock.patch.object(mk.platform, "system", lambda: system),
                      mock.patch.object(mk, "places_taken", lambda: set(taken))):
            patch.start()
            self.addCleanup(patch.stop)


class Offer(Redirected):
    """When the settings window should raise the subject."""

    def test_a_fresh_mac_is_offered_the_desktop_and_the_dock(self):
        self.on("Darwin")
        offer = mk.offer()
        self.assertTrue(offer["ask"])
        self.assertEqual([p["id"] for p in offer["places"]], ["desktop", "dock"])
        # Only the first is ticked; the Dock is more of an imposition.
        self.assertEqual([p["on"] for p in offer["places"]], [True, False])
        self.assertIn("Dock", offer["note"])

    def test_a_fresh_windows_machine_is_offered_the_start_menu(self):
        self.on("Windows")
        offer = mk.offer()
        self.assertEqual([p["id"] for p in offer["places"]],
                         ["desktop", "startmenu"])
        self.assertEqual([p["label"] for p in offer["places"]],
                         ["Desktop", "Start menu"])

    def test_nothing_is_offered_where_no_shortcut_can_be_made(self):
        self.on("Linux")
        self.assertEqual(mk.offer(), {"ask": False})

    def test_having_one_already_is_the_end_of_the_matter(self):
        self.on("Darwin", taken=["desktop"])
        self.assertEqual(mk.offer(), {"ask": False})

    def test_a_dock_icon_alone_also_counts_as_having_one(self):
        self.on("Darwin", taken=["dock"])
        self.assertEqual(mk.offer(), {"ask": False})

    def test_dont_ask_again_is_the_end_of_it_too(self):
        self.on("Darwin")
        self.assertTrue(mk.offer()["ask"])
        mk.stop_asking()
        self.assertEqual(mk.offer(), {"ask": False})

    def test_asking_for_the_sheet_outright_beats_dont_ask_again(self):
        # The only ways back used to be the Add to Desktop launcher and a
        # command-line flag. Now the window has a button, and it has to answer.
        self.on("Darwin")
        mk.stop_asking()
        offer = mk.offer(force=True)
        self.assertTrue(offer["ask"])
        self.assertEqual([p["id"] for p in offer["places"]], ["desktop", "dock"])

    def test_a_place_that_already_has_one_is_listed_and_says_so(self):
        self.on("Darwin", taken=["desktop"])
        offer = mk.offer(force=True)
        self.assertTrue(offer["ask"])
        desktop, dock = offer["places"]
        self.assertIn("already", desktop["label"])
        self.assertNotIn("already", dock["label"])
        # The tick starts somewhere that would actually be new.
        self.assertEqual([desktop["on"], dock["on"]], [False, True])

    def test_asking_outright_still_cannot_conjure_a_place(self):
        self.on("Linux")
        self.assertEqual(mk.offer(force=True), {"ask": False})

    def test_nothing_is_ticked_when_everywhere_has_one_already(self):
        self.on("Darwin", taken=["desktop", "dock"])
        offer = mk.offer(force=True)
        self.assertTrue(offer["ask"])
        self.assertEqual([p["on"] for p in offer["places"]], [False, False])

    def test_the_record_survives_being_written_twice(self):
        mk.save_state(added={"desktop": "/somewhere"})
        mk.stop_asking()
        state = json.loads(mk.STATE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(state, {"added": {"desktop": "/somewhere"},
                                 "never_ask": True})

    def test_an_unwritable_folder_is_not_a_failure(self):
        # Being unable to remember the answer is a smaller problem than refusing
        # to do the thing that was asked for.
        with mock.patch.object(mk, "STATE_PATH", Path("/nope/nowhere.json")):
            mk.stop_asking()

    def test_a_corrupt_record_reads_as_no_record(self):
        mk.STATE_PATH.write_text("{ this is not json", encoding="utf-8")
        self.assertEqual(mk.load_state(), {})


class Dock(unittest.TestCase):
    """The Dock rewrites what it's given, so reading it back needs care."""

    def read_returns(self, text):
        return mock.patch.object(
            mk.subprocess, "run",
            return_value=subprocess.CompletedProcess([], 0, text, ""))

    def test_a_tile_is_recognised_after_the_dock_has_rewritten_it(self):
        # What goes in is a path; what comes back out is a percent-encoded URL.
        # Searching the raw text for the name would miss it and add a second.
        with self.read_returns(
                '{"_CFURLString" = "file:///Users/x/Applications/'
                'Faceplace%20Marketbook.app/";}'):
            self.assertTrue(mk.in_dock())

    def test_a_tile_is_recognised_in_the_form_it_was_written_in(self):
        with self.read_returns(
                '{"_CFURLString" = "/Users/x/Applications/'
                'Faceplace Marketbook.app";}'):
            self.assertTrue(mk.in_dock())

    def test_somebody_elses_dock_is_left_alone(self):
        with self.read_returns('{"_CFURLString" = "file:///Applications/Mail.app/";}'):
            self.assertFalse(mk.in_dock())

    def test_a_dock_that_cannot_be_read_reads_as_empty(self):
        with mock.patch.object(
                mk.subprocess, "run",
                return_value=subprocess.CompletedProcess([], 1, "", "nope")):
            self.assertFalse(mk.in_dock())

    def test_the_tile_it_writes_is_a_plist_the_dock_can_parse(self):
        tile = mk.DOCK_TILE.format(path="/Users/x/Applications/App.app")
        parsed = plistlib.loads(
            f'<?xml version="1.0"?><plist version="1.0">{tile}</plist>'.encode())
        self.assertEqual(
            parsed["tile-data"]["file-data"],
            {"_CFURLString": "/Users/x/Applications/App.app",
             "_CFURLStringType": 0})

    def test_an_ampersand_in_a_home_folder_does_not_break_the_plist(self):
        tile = mk.DOCK_TILE.format(path=mk.html.escape("/Users/a&b/App.app"))
        parsed = plistlib.loads(
            f'<?xml version="1.0"?><plist version="1.0">{tile}</plist>'.encode())
        self.assertEqual(parsed["tile-data"]["file-data"]["_CFURLString"],
                         "/Users/a&b/App.app")


class Windows(unittest.TestCase):
    """Where a Windows machine keeps its desktop and Start menu."""

    def test_a_onedrive_desktop_is_looked_for_as_well_as_the_plain_one(self):
        # OneDrive moves the desktop and leaves the original folder in place, so
        # a shortcut can be sitting in either.
        with mock.patch.dict(mk.os.environ,
                             {"USERPROFILE": r"C:\Users\x",
                              "OneDrive": r"C:\Users\x\OneDrive"}, clear=True):
            with mock.patch.object(Path, "is_dir", lambda self: True):
                found = [str(p) for p in mk.windows_desktops()]
        self.assertEqual(len(found), 2)
        self.assertTrue(any("OneDrive" in p for p in found))

    def test_the_start_menu_is_the_users_own_not_the_whole_machines(self):
        with mock.patch.dict(mk.os.environ,
                             {"APPDATA": r"C:\Users\x\AppData\Roaming"},
                             clear=True):
            start = mk.windows_start_menu()
        self.assertEqual(start.name, "Programs")
        self.assertIn("Roaming", str(start))

    def test_no_appdata_is_not_a_crash(self):
        with mock.patch.dict(mk.os.environ, {}, clear=True):
            self.assertIsNone(mk.windows_start_menu())

    def test_each_place_maps_to_a_folder_windows_can_name(self):
        for place in mk.PLACES["Windows"]:
            self.assertIn(place, mk.WINDOWS_FOLDERS)

    def test_a_quote_in_a_path_cannot_end_the_powershell_string(self):
        self.assertEqual(mk.powershell_quote("C:\\it's here"), "'C:\\it''s here'")


class Icons(unittest.TestCase):
    """The drawing, and the two binary formats written by hand."""

    def test_the_svg_keeps_its_own_settings_when_it_is_rewrapped(self):
        # fill="none" lives on the <svg> tag and is what stops every shape being
        # filled in black. Dropping it once already turned the icon into a blob.
        attributes, body = mk.artwork()
        self.assertIn('fill="none"', attributes)
        self.assertNotIn("width=", attributes)
        self.assertNotIn("height=", attributes)
        self.assertIn("<path", body)

    def test_a_windows_icon_fills_the_square_it_is_given(self):
        markup = mk.full_bleed(mk.artwork(), 256)
        self.assertIn('width="256"', markup)
        self.assertIn('fill="none"', markup)

    def test_a_mac_icon_sits_on_apples_grid_rather_than_edge_to_edge(self):
        # 824 of 1024, centred, is where macOS expects the rounded square. Edge
        # to edge looks a size too big beside everything else in the Dock.
        markup = mk.on_mac_grid(mk.artwork(), 512)
        self.assertIn('viewBox="0 0 1024 1024"', markup)
        self.assertIn('x="100" y="100" width="824" height="824"', markup)
        self.assertIn('width="512" height="512"', markup)

    def test_the_ico_it_writes_is_one_windows_can_read(self):
        pngs = {16: b"\x89PNG\r\n\x1a\n" + b"a" * 40,
                256: b"\x89PNG\r\n\x1a\n" + b"b" * 90}
        with TemporaryDirectory() as tmp:
            dest = Path(tmp) / "icon.ico"
            mk.write_ico(pngs, dest)
            raw = dest.read_bytes()
        reserved, kind, count = struct.unpack("<HHH", raw[:6])
        self.assertEqual((reserved, kind, count), (0, 1, 2))
        seen = {}
        for i in range(count):
            (w, h, colours, pad, planes, bpp,
             size, offset) = struct.unpack("<BBBBHHII", raw[6 + 16 * i:22 + 16 * i])
            self.assertEqual((colours, pad, planes, bpp), (0, 0, 1, 32))
            seen[w] = raw[offset:offset + size]
        # A 256-pixel image is recorded as 0, because the field is one byte.
        self.assertEqual(sorted(seen), [0, 16])
        self.assertEqual(seen[16], pngs[16])
        self.assertEqual(seen[0], pngs[256])

    def test_every_size_the_iconset_needs_is_one_that_gets_drawn(self):
        needed = {16, 32, 64, 128, 256, 512, 1024}
        self.assertEqual(set(mk.MAC_SIZES), needed)


class Wording(unittest.TestCase):
    """What the person is told afterwards."""

    def test_one_place_reads_as_a_sentence(self):
        self.assertEqual(mk.sentence(["desktop"]), "on your desktop")

    def test_two_places_are_joined_with_and(self):
        self.assertEqual(mk.sentence(["desktop", "dock"]),
                         "on your desktop and in your Dock")

    def test_a_place_that_did_not_work_is_reported_alongside_the_ones_that_did(self):
        message = mk.summary(["desktop"], ["The Dock said no."])
        self.assertIn("on your desktop", message)
        self.assertIn("The Dock said no.", message)

    def test_nothing_working_says_only_what_went_wrong(self):
        self.assertEqual(mk.summary([], ["The Dock said no."]),
                         "The Dock said no.")

    def test_every_place_has_a_label_and_a_phrase_for_a_message(self):
        for places in mk.PLACES.values():
            for place in places:
                self.assertIn(place, mk.PLACE_LABELS)
                self.assertIn(place, mk.PLACE_PHRASES)


class FromTheWindow(Redirected):
    """The settings window's route in, which runs the work in its own process
    because it's calling from inside a browser of its own."""

    def test_an_empty_tick_list_is_refused_without_starting_anything(self):
        self.on("Darwin")
        with mock.patch.object(mk.subprocess, "run") as run:
            answer = mk.add_from_ui([])
        self.assertIn("error", answer)
        run.assert_not_called()

    def test_a_place_this_computer_does_not_have_is_ignored(self):
        self.on("Darwin")
        with mock.patch.object(mk.subprocess, "run") as run:
            answer = mk.add_from_ui(["startmenu"])
        self.assertIn("error", answer)
        run.assert_not_called()

    def test_a_success_is_passed_back_and_ends_the_asking(self):
        self.on("Darwin")
        reply = json.dumps({"added": ["desktop"], "ok": True, "message": "Done."})
        with mock.patch.object(
                mk.subprocess, "run",
                return_value=subprocess.CompletedProcess([], 0, reply, "")):
            answer = mk.add_from_ui(["desktop"])
        self.assertEqual(answer["added"], ["desktop"])
        # Having said yes once, they shouldn't be asked again later just because
        # they tidied the icon away.
        self.assertTrue(mk.load_state().get("never_ask"))

    def test_a_failure_is_reported_and_leaves_the_question_open(self):
        self.on("Darwin")
        reply = json.dumps({"error": "iconutil fell over"})
        with mock.patch.object(
                mk.subprocess, "run",
                return_value=subprocess.CompletedProcess([], 1, reply, "")):
            answer = mk.add_from_ui(["desktop"])
        self.assertEqual(answer["error"], "iconutil fell over")
        self.assertFalse(mk.load_state().get("never_ask"))

    def test_a_process_that_says_nothing_useful_still_gets_a_message(self):
        self.on("Darwin")
        with mock.patch.object(
                mk.subprocess, "run",
                return_value=subprocess.CompletedProcess([], 1, "", "Killed")):
            answer = mk.add_from_ui(["desktop"])
        self.assertEqual(answer["error"], "Killed")

    def test_the_window_gets_the_four_hooks_it_looks_for(self):
        self.assertEqual(set(mk.ui_hooks()),
                         {"shortcut_offer", "shortcut_reopen", "add_shortcut",
                          "shortcut_never"})


if __name__ == "__main__":
    unittest.main()
