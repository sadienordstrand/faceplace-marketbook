#!/usr/bin/env python3
"""
Offline tests for the in-place updater.

    python3 -m unittest discover tests

Nothing here touches the network or the real project folder. Every test builds a
throwaway one, points the updater at it, and hands it a zip made on the spot —
so what's covered is the part that can't be undone by walking away: which files
an update is allowed to write, which it must never touch, and whether a folder
survives an update that dies half way through.
"""
import contextlib
import io
import json
import os
import ssl
import sys
import types
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import updater


def write(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_zip(files, top="faceplace-marketbook-main", modes=None):
    """A stand-in for what codeload sends back."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for relative, text in files.items():
            info = zipfile.ZipInfo(f"{top}/{relative}")
            info.external_attr = ((modes or {}).get(relative, 0o644) << 16)
            archive.writestr(info, text)
    return buf.getvalue()


def fake_scheduling(busy=False, what="a manual run", started=None):
    """A stand-in for the module that owns the run lock.

    The real one pulls in Playwright and writes its lock into the real project
    folder, neither of which belongs in an offline test. What matters here is
    only whether the lock is free, and whether the updater took it.
    """
    module = types.ModuleType("scheduling")

    class AlreadyRunning(Exception):
        def __init__(self, holder):
            super().__init__(f"{holder['what']} has been running since "
                             f"{holder['started']}")
            self.holder = holder

    class run_lock:
        taken = []

        def __init__(self, what):
            self.what = what

        def __enter__(self):
            if busy:
                raise AlreadyRunning({"what": what, "pid": 999,
                                      "started": started or "2026-08-09T16:55:00"})
            run_lock.taken.append(self.what)
            return self

        def __exit__(self, *exc):
            return False

    module.AlreadyRunning = AlreadyRunning
    module.run_lock = run_lock
    # Only the settings-window path asks for these, and it doesn't care what's
    # in them.
    module.ui_hooks = dict
    return module


# The smallest tree verify() will accept.
SHIPPED = {
    "src/fb_marketplace_sweep.py": "# version two\n",
    "src/version.py": '__version__ = "2.0.0"\n',
    "requirements.txt": "playwright>=1.40\n",
}


class Numbers(unittest.TestCase):
    """Comparing versions, which is why they aren't compared as text."""

    def test_a_later_release_wins(self):
        self.assertTrue(updater.is_newer("1.0.1", "1.0.0"))
        self.assertTrue(updater.is_newer("2.0", "1.9.9"))

    def test_the_tenth_release_comes_after_the_ninth(self):
        self.assertTrue(updater.is_newer("1.10", "1.9"))
        self.assertFalse(updater.is_newer("1.9", "1.10"))

    def test_the_same_version_is_not_newer(self):
        self.assertFalse(updater.is_newer("1.2.3", "1.2.3"))
        # Trailing zeros are the same number, written differently.
        self.assertFalse(updater.is_newer("1.2", "1.2.0"))

    def test_nonsense_never_offers_an_update(self):
        self.assertFalse(updater.is_newer("", "1.0.0"))
        self.assertFalse(updater.is_newer(None, "1.0.0"))


class Redirected(unittest.TestCase):
    """Points the updater at a throwaway project folder."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, value in (("ROOT", self.root),
                            ("UPDATE_DIR", self.root / ".state" / "update"),
                            ("UPDATE_STATE_PATH",
                             self.root / ".state" / "update.json"),
                            ("__version__", "1.0.0"),
                            # Each test is its own launch, so each starts having
                            # asked nobody anything.
                            ("_asked", updater._UNASKED),
                            ("_reached", False)):
            patch = mock.patch.object(updater, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        (self.root / ".state").mkdir()
        # A launcher that can restart the app announces itself here, and the
        # shell running these tests may well have one. Each test says for itself
        # whether anything is listening.
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(updater.RELAUNCH_ENV, None)
        self.free_lock()

    def free_lock(self, busy=False, **holder):
        """Nothing is running, unless a test says otherwise."""
        self.scheduling = fake_scheduling(busy, **holder)
        patch = mock.patch.dict(sys.modules, {"scheduling": self.scheduling})
        patch.start()
        self.addCleanup(patch.stop)
        return self.scheduling

    def populate(self):
        """A project folder as someone who unpacked the zip would have it."""
        for relative, text in SHIPPED.items():
            write(self.root, relative, text.replace("two", "one")
                  .replace("2.0.0", "1.0.0"))
        write(self.root, "src/ui/settings.css", "/* one */\n")
        write(self.root, "docs/how-it-works.md", "# one\n")
        write(self.root, ".state/saved_searches.json", '{"searches": []}')
        write(self.root, "runs/defender_2026-01-01/gallery.html", "<html>")


class Asking(Redirected):
    """When the repository gets asked, and what a failed ask falls back to."""

    def test_a_fresh_answer_is_fetched_even_though_one_is_remembered(self):
        # Not "at most once a day". The update worth telling someone about is
        # usually the one they were just told to go and get.
        updater.save_state(last_check=datetime.now().isoformat(),
                           latest="1.5.0")
        with mock.patch.object(updater, "latest_version", return_value="1.6.0"):
            self.assertEqual(updater.check(), "1.6.0")
        self.assertEqual(updater.load_state()["latest"], "1.6.0")

    def test_one_launch_asks_once_however_many_things_want_to_know(self):
        # The terminal line and the window banner both call this. Two lookups
        # would mean two waits on a connection that isn't answering.
        with mock.patch.object(updater, "latest_version",
                               return_value="1.6.0") as asked:
            updater.check()
            updater.check()
            updater.available()
        self.assertEqual(asked.call_count, 1)

    def test_the_command_line_can_insist_on_a_fresh_answer(self):
        with mock.patch.object(updater, "latest_version",
                               return_value="1.6.0") as asked:
            updater.check()
            updater.check(force=True)
        self.assertEqual(asked.call_count, 2)

    def test_being_offline_falls_back_to_the_last_answer(self):
        # Which is what keeps the banner up for someone who heard about an
        # update yesterday and is on a train today.
        updater.save_state(latest="1.5.0")
        with mock.patch.object(updater, "latest_version", return_value=None):
            self.assertEqual(updater.check(), "1.5.0")
        self.assertEqual(updater.load_state()["latest"], "1.5.0")


class Offering(Redirected):
    """Whether the settings window says anything."""

    def offer(self, newest):
        with mock.patch.object(updater, "check", return_value=newest):
            return updater.available()

    def test_a_newer_version_is_offered(self):
        offer = self.offer("1.1.0")
        self.assertTrue(offer["show"])
        self.assertEqual(offer["version"], "1.1.0")
        self.assertEqual(offer["current"], "1.0.0")

    def test_the_newest_version_says_nothing(self):
        self.assertFalse(self.offer("1.0.0")["show"])

    def test_the_same_version_is_offered_again_on_the_next_launch(self):
        # "Not now" hides the banner for that window only. Nothing about the
        # refusal is written down, so putting an update off doesn't quietly
        # become never taking it.
        self.assertTrue(self.offer("1.1.0")["show"])
        self.assertTrue(self.offer("1.1.0")["show"])
        self.assertNotIn("skipped", updater.load_state())

    def test_a_clone_is_left_alone(self):
        (self.root / ".git").mkdir()
        self.assertFalse(self.offer("9.9.9")["show"])
        self.assertIn("git pull", updater.update_now()["error"])


class Unpacking(Redirected):
    """What arrives over the wire, before any of it is believed."""

    def unpack(self, data):
        scratch = Path(self.tmp.name) / "scratch"
        scratch.mkdir(exist_ok=True)
        return updater.extract(data, scratch)

    def test_the_project_folder_inside_the_zip_is_found(self):
        tree = self.unpack(make_zip(SHIPPED))
        self.assertEqual(updater.verify(tree), "2.0.0")

    def test_a_launcher_comes_out_still_runnable(self):
        files = {**SHIPPED, "Start Faceplace Marketbook (Mac).command": "#!/bin/bash\n"}
        tree = self.unpack(make_zip(
            files, modes={"Start Faceplace Marketbook (Mac).command": 0o755}))
        launcher = tree / "Start Faceplace Marketbook (Mac).command"
        # zipfile drops the executable bit unless it's put back by hand, and a
        # launcher that isn't executable can't be double-clicked.
        self.assertTrue(launcher.stat().st_mode & 0o111)

    def test_something_that_isnt_a_zip_is_refused(self):
        with self.assertRaises(updater.UpdateFailed):
            self.unpack(b"<html>sign in to the hotel wifi</html>")

    def test_a_zip_that_isnt_one_project_folder_is_refused(self):
        loose = io.BytesIO()
        with zipfile.ZipFile(loose, "w") as archive:
            archive.writestr("one/a.txt", "a")
            archive.writestr("two/b.txt", "b")
        with self.assertRaises(updater.UpdateFailed):
            self.unpack(loose.getvalue())

    def test_a_tree_missing_the_program_is_refused(self):
        tree = self.unpack(make_zip({"README.md": "hello"}))
        with self.assertRaises(updater.UpdateFailed):
            updater.verify(tree)


class Choosing(Redirected):
    """Which files an update is allowed to write and delete."""

    def setUp(self):
        super().setUp()
        self.populate()
        self.tree = updater.extract(
            make_zip(SHIPPED), Path(self.tmp.name) / "scratch")

    def test_the_users_own_folders_are_never_shipped_over(self):
        # They can't be in the zip anyway — they're not in the repository — but
        # a stray one must not be picked up if they ever were.
        write(self.tree, ".state/saved_searches.json", "{}")
        write(self.tree, "runs/old/gallery.html", "<html>")
        shipped = updater.shipped_files(self.tree)
        self.assertNotIn(Path(".state/saved_searches.json"), shipped)
        self.assertNotIn(Path("runs/old/gallery.html"), shipped)

    def test_dropped_modules_are_cleaned_up(self):
        write(self.root, "src/old_module.py", "# gone upstream\n")
        stale = updater.stale_files(updater.shipped_files(self.tree))
        self.assertIn(Path("src/old_module.py"), stale)

    def test_nothing_outside_the_code_folders_is_ever_deleted(self):
        write(self.root, "my notes.txt", "where I left off")
        write(self.root, ".state/email_config.json", "{}")
        stale = updater.stale_files(updater.shipped_files(self.tree))
        self.assertEqual([p for p in stale if p.parts[0] not in ("src", "docs")],
                         [])

    def test_compiled_leftovers_are_not_mistaken_for_dropped_modules(self):
        write(self.root, "src/__pycache__/paths.cpython-311.pyc", "x")
        stale = updater.stale_files(updater.shipped_files(self.tree))
        self.assertNotIn(Path("src/__pycache__/paths.cpython-311.pyc"), stale)


class Applying(Redirected):
    """The part that can't be undone by walking away."""

    def setUp(self):
        super().setUp()
        self.populate()
        self.tree = updater.extract(
            make_zip(SHIPPED), Path(self.tmp.name) / "scratch")

    def snapshot(self):
        return {p.relative_to(self.root): p.read_bytes()
                for p in sorted(self.root.rglob("*"))
                if p.is_file() and ".state" not in p.parts}

    def test_the_code_is_replaced(self):
        updater.apply(self.tree)
        self.assertEqual((self.root / "src" / "version.py").read_text(),
                         '__version__ = "2.0.0"\n')

    def test_what_the_user_made_is_left_where_it_was(self):
        before = (self.root / ".state" / "saved_searches.json").read_bytes()
        gallery = self.root / "runs" / "defender_2026-01-01" / "gallery.html"
        updater.apply(self.tree)
        self.assertEqual(
            (self.root / ".state" / "saved_searches.json").read_bytes(), before)
        self.assertTrue(gallery.exists())

    def test_a_module_the_new_version_dropped_is_removed(self):
        gone = write(self.root, "src/old_module.py", "# dropped\n")
        updater.apply(self.tree)
        self.assertFalse(gone.exists())

    def test_a_file_of_the_users_own_is_not(self):
        theirs = write(self.root, "my notes.txt", "where I left off")
        updater.apply(self.tree)
        self.assertEqual(theirs.read_text(), "where I left off")

    def test_an_update_that_dies_half_way_leaves_the_folder_as_it_was(self):
        write(self.root, "src/old_module.py", "# dropped\n")
        before = self.snapshot()
        real = updater._install
        done = []

        def flaky(source, destination):
            done.append(destination)
            if len(done) == 2:
                raise OSError(28, "No space left on device")
            return real(source, destination)

        with mock.patch.object(updater, "_install", flaky):
            with self.assertRaises(OSError):
                updater.apply(self.tree)
        self.assertEqual(self.snapshot(), before)

    def test_nothing_is_left_behind_to_be_explained(self):
        updater.apply(self.tree)
        self.assertFalse((self.root / ".state" / "update" / "previous").exists())
        leftovers = [p.name for p in self.root.rglob("*.new")]
        self.assertEqual(leftovers, [])


class Reporting(Redirected):
    """What the settings window is told, given the whole thing runs behind a
    button that has to say something either way."""

    def test_a_download_that_fails_changes_nothing_and_says_why(self):
        self.populate()
        before = (self.root / "src" / "version.py").read_bytes()
        with mock.patch.object(updater, "fetch",
                               side_effect=updater.URLError("no route to host")):
            answer = updater.update_now()
        self.assertIn("internet connection", answer["error"])
        self.assertEqual((self.root / "src" / "version.py").read_bytes(), before)

    def test_a_mac_with_no_certificates_is_told_how_to_fix_it(self):
        # urlopen hands back the certificate failure wrapped in a URLError, so
        # reading only the outer type would give the wrong advice entirely.
        wrapped = updater.URLError(
            ssl.SSLCertVerificationError(
                "certificate verify failed: unable to get local issuer"))
        self.assertIn("Install Certificates", updater.in_plain_words(wrapped))

    def test_a_good_download_reports_the_new_version(self):
        self.populate()
        with mock.patch.object(updater, "fetch", return_value=make_zip(SHIPPED)):
            answer = updater.update_now()
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["version"], "2.0.0")
        self.assertIn("2.0.0", answer["message"])


class Reporting(Redirected):
    """The terminal line, which says something on every launch.

    Speaking up only when there was news is what made an up-to-date copy
    indistinguishable from a check that failed quietly, and from one that never
    ran at all. So each of the reasons there's nothing to offer has its own
    words, and this is where they're pinned down. This copy is 1.0.0 throughout.
    """

    def said(self, remote):
        """announce(), with the repository answering `remote` — None for a
        connection that didn't. Whitespace flattened so the assertions below
        don't depend on where the lines wrap."""
        with mock.patch.object(updater, "latest_version", return_value=remote):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                updater.announce()
        return " ".join(out.getvalue().split())

    def test_the_check_is_announced_before_it_is_made(self):
        # The line goes up first, so the wait on a connection that's timing out
        # happens under an explanation of itself rather than under nothing.
        seen = []

        def answering():
            seen.append(sys.stdout.getvalue())
            return "1.0.0"

        with mock.patch.object(updater, "latest_version", answering):
            with contextlib.redirect_stdout(io.StringIO()):
                updater.announce()
        self.assertIn("Checking for updates", seen[0])

    def test_being_up_to_date_is_said_out_loud(self):
        said = self.said("1.0.0")
        self.assertIn("Checking for updates", said)
        self.assertIn("up to date", said)
        self.assertIn("1.0.0", said)

    def test_a_newer_version_is_reported_with_somewhere_to_go(self):
        said = self.said("1.1.0")
        self.assertIn("1.1.0 is available", said)
        self.assertIn("This is 1.0.0", said)
        self.assertIn("window", said)

    def test_a_copy_ahead_of_the_repository_is_told_so_and_why(self):
        # The case that sent someone looking for this line: the version bumped
        # and pushed, and the CDN in front of raw.githubusercontent still handing
        # out the file from before the push.
        said = self.said("0.9.0")
        self.assertIn("ahead of the repository", said)
        self.assertIn("0.9.0", said)
        self.assertIn("few minutes", said)

    def test_a_failed_check_says_so_rather_than_nothing(self):
        said = self.said(None)
        self.assertIn("Couldn't reach GitHub", said)

    def test_an_answer_from_memory_is_not_passed_off_as_a_fresh_one(self):
        updater.save_state(latest="1.1.0")
        said = self.said(None)
        self.assertIn("1.1.0 is available", said)
        self.assertIn("the last launch heard", said)

    def test_a_clone_hears_why_it_is_being_left_alone(self):
        (self.root / ".git").mkdir()
        with mock.patch.object(updater, "latest_version") as asked:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                updater.announce()
        self.assertIn("git pull", out.getvalue())
        # And no seconds are spent on a question whose answer it can't act on.
        asked.assert_not_called()


class Restarting(Redirected):
    """Getting onto the new version, which takes a new process to load it.

    Installing leaves this one running the code it read at startup, so the app
    has to be started again. The launcher is the only thing that can do that
    properly — it reinstalls libraries on the way past — and it says so by
    setting an environment variable naming the exit code it watches for.
    """

    def test_nothing_listening_means_no_restart_to_offer(self):
        self.assertIsNone(updater.relaunch_code())

    def test_a_launcher_names_the_code_it_watches_for(self):
        with mock.patch.dict(os.environ, {updater.RELAUNCH_ENV: "75"}):
            self.assertEqual(updater.relaunch_code(), 75)

    def test_a_code_that_couldnt_work_counts_as_nothing_listening(self):
        # Zero would read as "finished normally", and neither shell brings
        # anything above 255 back intact.
        for value in ("", "0", "256", "yes", "75 please"):
            with mock.patch.dict(os.environ, {updater.RELAUNCH_ENV: value}):
                self.assertIsNone(updater.relaunch_code(), value)

    def install(self, install=None):
        """Update a populated folder, with the download stubbed out."""
        self.populate()
        with contextlib.ExitStack() as patched:
            patched.enter_context(mock.patch.object(updater, "fetch",
                                                    return_value=make_zip(SHIPPED)))
            if install:
                patched.enter_context(mock.patch.object(updater, "_install",
                                                        install))
            return updater.update_now()

    def test_a_restart_is_promised_only_when_one_is_actually_coming(self):
        with mock.patch.dict(os.environ, {updater.RELAUNCH_ENV: "75"}):
            answer = self.install()
        self.assertTrue(answer["restart"])
        self.assertIn("closes on its own", answer["message"])

    def test_with_no_launcher_it_asks_to_be_started_again_by_hand(self):
        answer = self.install()
        self.assertFalse(answer["restart"])
        self.assertIn("start Faceplace Marketbook again", answer["message"])

    def test_a_window_with_something_to_say_waits_to_be_dismissed(self):
        # A note means the update didn't go entirely to plan. Closing on a timer
        # would carry the note off before it could be read.
        stubborn = mock.Mock(side_effect=lambda source, destination:
                             destination.parent != updater.ROOT)
        with mock.patch.dict(os.environ, {updater.RELAUNCH_ENV: "75"}):
            answer = self.install(install=stubborn)
        self.assertTrue(answer["notes"])
        self.assertIn("Choose Restart now", answer["message"])

    def test_the_command_line_never_promises_what_it_cant_do(self):
        # Nothing reads what `updater.py --update` exits with, even in a shell
        # that happens to have the variable set.
        self.populate()
        with mock.patch.dict(os.environ, {updater.RELAUNCH_ENV: "75"}):
            with mock.patch.object(updater, "fetch",
                                   return_value=make_zip(SHIPPED)):
                answer = updater.update_now(restart=False)
        self.assertIn("start Faceplace Marketbook again", answer["message"])

    def close_window_on(self, answer):
        """Run the settings-window path with the window already answered."""
        import fb_marketplace_sweep as fb

        window = types.SimpleNamespace(collect_settings=lambda *a, **k: answer)
        icons = types.SimpleNamespace(ui_hooks=dict)
        cities = types.SimpleNamespace(load_locations=dict, base_locations=dict)
        asked = types.SimpleNamespace(query="", exclude="", pace=None)
        with mock.patch.dict(sys.modules, {"settings_ui": window,
                                           "make_desktop_icon": icons}):
            with mock.patch.object(fb, "locations", cities):
                fb.run_from_ui(asked)

    def test_closing_the_window_after_an_update_exits_with_the_code(self):
        with mock.patch.dict(os.environ, {updater.RELAUNCH_ENV: "75"}):
            with mock.patch("builtins.print"):
                with self.assertRaises(SystemExit) as leaving:
                    self.close_window_on({"action": "updated"})
        self.assertEqual(leaving.exception.code, 75)

    def test_with_no_launcher_it_just_stops(self):
        # Exiting 75 into a launcher too old to know what it means would only
        # get "Faceplace exited with an error (code 75)" printed at someone.
        with mock.patch("builtins.print") as said:
            self.close_window_on({"action": "updated"})
        self.assertIn("Start Faceplace Marketbook again",
                      " ".join(str(c) for c in said.call_args_list))


class WhileSomethingIsRunning(Redirected):
    """A sweep and an update must never overlap in one folder.

    A run that's been going an hour has most of the program loaded but not all
    of it — the gallery builder is imported at the very end — so an update
    landing underneath one would have it finish on a mix of two versions.
    """

    def test_an_update_waits_for_a_search_that_is_already_going(self):
        self.populate()
        self.free_lock(busy=True)
        before = (self.root / "src" / "version.py").read_bytes()
        with mock.patch.object(updater, "fetch", return_value=make_zip(SHIPPED)):
            answer = updater.update_now()
        self.assertIn("A search is running in this folder", answer["error"])
        self.assertIn("It started at 4:55pm", answer["error"])
        self.assertIn("Let it finish", answer["error"])
        self.assertEqual((self.root / "src" / "version.py").read_bytes(), before)

    def test_a_saved_search_reads_the_same_as_a_manual_one(self):
        # The lock calls them "scheduled run" and "a manual run", which are
        # written for a log file. To whoever is looking at the window it's a
        # search either way, and phrasing it from the raw string would produce
        # "scheduled run has been running since…" in the middle of a sentence.
        said = []
        for what in ("a manual run", "scheduled run"):
            self.free_lock(busy=True, what=what)
            with mock.patch.object(updater, "fetch", return_value=b""):
                said.append(updater.update_now()["error"])
        self.assertEqual(said[0], said[1])
        self.assertNotIn("scheduled run", said[1])

    def test_a_lock_with_no_readable_start_time_still_explains_itself(self):
        self.free_lock(busy=True, started="not a timestamp")
        with mock.patch.object(updater, "fetch", return_value=b""):
            error = updater.update_now()["error"]
        self.assertIn("A search is running in this folder", error)
        self.assertNotIn("It started at", error)

    def test_the_lock_is_held_for_the_whole_update(self):
        # So a scheduled search that comes due mid-update finds it taken, skips,
        # and tries again later — which is what it already does for a manual run.
        self.populate()
        self.scheduling.run_lock.taken.clear()
        with mock.patch.object(updater, "fetch", return_value=make_zip(SHIPPED)):
            self.assertTrue(updater.update_now()["ok"])
        self.assertEqual(self.scheduling.run_lock.taken, ["an update"])

    def test_starting_a_search_mid_update_is_told_what_is_in_the_way(self):
        # The other direction. The lock's usual explanation — that both runs
        # want the one Facebook session — is untrue when an update is holding
        # it, and an update clears in a minute rather than an hour.
        import fb_marketplace_sweep as fb

        def blocked_by(what):
            return self.scheduling.AlreadyRunning(
                {"what": what, "pid": 1, "started": "2026-08-09T16:55:00"})

        self.assertIn("An update is replacing", fb.why_wait(blocked_by("an update")))
        self.assertIn("Facebook session", fb.why_wait(blocked_by("a manual run")))

    def test_a_scheduler_that_wont_import_doesnt_block_the_fix_for_it(self):
        self.populate()
        with mock.patch.dict(sys.modules, {"scheduling": None}):
            with mock.patch.object(updater, "fetch",
                                   return_value=make_zip(SHIPPED)):
                self.assertTrue(updater.update_now()["ok"])


class StateFile(Redirected):
    """The notes kept between launches."""

    def test_a_corrupt_note_is_ignored_rather_than_fatal(self):
        updater.UPDATE_STATE_PATH.write_text("{not json", encoding="utf-8")
        self.assertEqual(updater.load_state(), {})
        updater.save_state(latest="1.1.0")
        self.assertEqual(json.loads(updater.UPDATE_STATE_PATH.read_text())
                         ["latest"], "1.1.0")


if __name__ == "__main__":
    unittest.main()
