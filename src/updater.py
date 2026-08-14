#!/usr/bin/env python3
"""
updater.py
----------
Keeps a copy that arrived as a downloaded zip in step with the repository.

Most people who have this never cloned it — they were sent a link, clicked
Download ZIP, and unpacked it. There's no git in that folder to pull with, so
this module does the same job by hand: it asks the repository what the newest
version is, tells the owner when theirs is behind, and replaces the code in
place when they ask it to.

In place is the point. Everything a person would hate to lose lives in .state/
(the Facebook login, scheduled searches, the database, the email settings) and in
runs/, both of which this never touches — so updating keeps the login, the
history, and any desktop shortcut pointing at this folder.

  version.py     the number this copy is at, and the one the repository is at
  .state/update.json   when we last asked and what the answer was

The check runs on every launch and swallows every error. On a working connection
it costs about a third of a second; on a broken one it costs CHECK_TIMEOUT and
then says nothing, which is a delay worth wearing — a machine that can't reach
GitHub in two seconds is about to have a far worse time driving Facebook.

Installing doesn't put this process on the new version — Python read the old
files into memory minutes ago and won't read them again. So the app has to be
started afresh, and rather than ask someone to go and find the launcher, it asks
the launcher to do it. See relaunch_code().

A folder with a .git in it is left alone entirely. That's a clone, its owner has
git, and unpacking a zip over the top of their working tree would throw away
whatever they hadn't committed.
"""
import argparse
import contextlib
import io
import json
import os
import re
import shutil
import ssl
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from paths import ROOT, UPDATE_DIR, UPDATE_STATE_PATH
from version import __version__

REPO = "sadienordstrand/faceplace-marketbook"
BRANCH = "main"

# The version number is read straight out of the file rather than from a release
# or a tag, so pushing a bumped version.py is all it takes to offer an update.
# raw.githubusercontent serves it through a CDN that holds a copy for a few
# minutes, so a push can take that long to become visible.
VERSION_URL = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/src/version.py"
ZIP_URL = f"https://codeload.github.com/{REPO}/zip/refs/heads/{BRANCH}"

# The whole budget for the startup check, which sits between a double-click and
# the window appearing. A working connection answers in about a third of a
# second; this is what a broken one costs instead. Worth paying on every launch:
# a machine that can't reach GitHub in two seconds is about to have a much worse
# time driving Facebook, so the delay is the least of that person's problems.
CHECK_TIMEOUT = 2
DOWNLOAD_TIMEOUT = 120
# The whole repository is a couple of megabytes. Anything remotely near this
# means something other than our zip is on the end of that connection.
MAX_ZIP_BYTES = 60 * 1024 * 1024

# Never read out of the downloaded copy and never written over in this one.
# Only .git is actually capable of appearing in the zip; the rest are here to
# say plainly what an update is not allowed to disturb.
PROTECTED = frozenset({".state", "runs", ".venv", ".git"})

# Where a file this version no longer ships gets deleted rather than left
# behind. Confined to the folders that hold nothing but code and its assets, so
# an update can never remove something of the user's own. The project root is
# left alone: it's the folder they keep things in.
PRUNED = ("src", "docs")

VERSION_RE = re.compile(r"""__version__\s*=\s*["']([^"']+)["']""")


class UpdateFailed(Exception):
    """Something went wrong that the user should be told about in words."""


class Busy(Exception):
    """A sweep is using this folder, so now is not the time."""


# --- Version numbers ---------------------------------------------------------

def as_numbers(text):
    """"1.10.2" -> (1, 10, 2). Compared a number at a time so 1.10 lands after
    1.9, which is the whole reason this isn't a string comparison."""
    return tuple(int(n) for n in re.findall(r"\d+", text or "")) or (0,)


def is_newer(remote, local):
    a, b = as_numbers(remote), as_numbers(local)
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


def managed_by_git():
    return (ROOT / ".git").exists()


# --- What we remember between launches ---------------------------------------

def load_state():
    try:
        data = json.loads(UPDATE_STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(**changes):
    state = {**load_state(), **changes}
    try:
        tmp = UPDATE_STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(UPDATE_STATE_PATH)
    except OSError:
        pass  # a folder we can't write to is not a reason to stop
    return state


# --- Talking to GitHub -------------------------------------------------------

def _certificates():
    """A CA bundle for the Macs that haven't got one.

    Python from python.org installs its own copy of OpenSSL with no certificates
    wired up until you run the "Install Certificates.command" that ships beside
    it, and plenty of people never do. Playwright downloads its browser through
    Node, so this is the first thing in the app that would notice.
    """
    try:
        import certifi
    except ImportError:
        return None
    try:
        return ssl.create_default_context(cafile=certifi.where())
    except (OSError, ssl.SSLError):
        return None


def about_certificates(error):
    """Whether this failure is the missing-CA-bundle one.

    urlopen doesn't let the SSL error through: everything OSError-shaped that
    happens while connecting comes back wrapped in a URLError, with the real one
    hanging off `reason`. So both the retry below and the advice in
    in_plain_words have to look inside rather than catch the obvious type.
    """
    if isinstance(error, HTTPError):
        return False
    inner = getattr(error, "reason", None) if isinstance(error, URLError) else error
    return isinstance(inner, ssl.SSLCertVerificationError)


def fetch(url, timeout, limit=None):
    """The bytes at `url`, or an exception. `limit` caps how many we'll take."""
    request = Request(url, headers={
        "User-Agent": f"faceplace-marketbook/{__version__}"})

    def read(context):
        with urlopen(request, timeout=timeout, context=context) as response:
            # One byte past the limit, so going over is detectable rather than
            # silently truncated into a corrupt zip.
            body = response.read() if limit is None else response.read(limit + 1)
        if limit is not None and len(body) > limit:
            raise UpdateFailed("The download was far bigger than it should be, "
                               "so it was abandoned.")
        return body

    try:
        return read(ssl.create_default_context())
    except URLError as e:
        fallback = _certificates() if about_certificates(e) else None
        if fallback is None:
            raise
        return read(fallback)


def in_plain_words(error):
    """Network failures, said the way you'd say them out loud."""
    if isinstance(error, UpdateFailed):
        return str(error)
    if about_certificates(error):
        return ("This computer's Python can't check secure connections yet. "
                "Open your Applications folder, then the Python folder inside "
                "it, and double-click \"Install Certificates.command\". Then "
                "try again.")
    if isinstance(error, HTTPError):
        return f"GitHub answered with an error ({error.code}). Try again later."
    if isinstance(error, URLError):
        return "Couldn't reach GitHub. Check your internet connection."
    return f"{type(error).__name__}: {error}"


# --- Is there a new version? -------------------------------------------------

def latest_version(timeout=CHECK_TIMEOUT):
    """The version the repository is at, or None if we couldn't find out."""
    try:
        body = fetch(VERSION_URL, timeout, limit=64 * 1024)
    except Exception:
        return None
    found = VERSION_RE.search(body.decode("utf-8", "replace"))
    return found.group(1) if found else None


# What this launch was told, so the two things that want to know — the line in
# the terminal and the banner in the window — don't ask twice. The sentinel is
# its own object rather than None because None is itself an answer: it's what a
# launch with no connection and nothing remembered ends up with, and treating
# that as "not asked yet" would spend CHECK_TIMEOUT again on every lookup.
_UNASKED = object()
_asked = _UNASKED
# Whether that answer came from the repository just now or from what an earlier
# launch wrote down. Worth telling apart out loud: "up to date" on the strength
# of yesterday's answer is a weaker claim than it sounds.
_reached = False


def check(force=False):
    """Ask the repository what the newest version is. Once per launch.

    Not once a day. The notice most worth showing is the one about an update
    whose author has just told you to go and get it, and an answer cached this
    morning can't carry that news. So it asks every time, and the cost of asking
    is CHECK_TIMEOUT at the very worst.

    Returns the newest version we know of. An answer we couldn't get falls back
    to the last one we did, which is what keeps the banner up on a machine that
    heard about an update yesterday and is offline today.
    """
    global _asked, _reached
    if _asked is not _UNASKED and not force:
        return _asked
    fresh = latest_version()
    _reached = fresh is not None
    _asked = fresh or load_state().get("latest")
    save_state(last_check=datetime.now().isoformat(timespec="seconds"),
               latest=_asked)
    return _asked


def available(force=False):
    """What the settings window needs to decide whether to say anything.

    `show` is the whole of what the window reads. `why` is for the terminal
    line, which says something either way and so needs to tell apart the several
    different reasons there's nothing to offer — they were all one silence
    before, and that silence was impossible to tell from a check that never ran.
    """
    here = {"current": __version__}
    if managed_by_git():
        return {"show": False, "why": "clone", **here}
    newest = check(force=force)
    if not newest:
        return {"show": False, "why": "unreachable", **here}
    found = {**here, "version": newest, "checked": _reached}
    if is_newer(__version__, newest):
        # Only ever happens to whoever is writing this: the version has been
        # bumped here and not pushed, or was pushed a minute ago and the CDN in
        # front of raw.githubusercontent is still handing out the old file.
        return {"show": False, "why": "ahead", **found}
    if not is_newer(newest, __version__):
        return {"show": False, "why": "current", **found}
    return {"show": True, "why": "newer", **found}


# --- Fetching and checking the new copy --------------------------------------

def extract(data, into):
    """Unpack the downloaded zip and return the project folder inside it.

    GitHub wraps everything in a single top-level folder named for the repo and
    the branch, so what's returned is that folder, not `into`.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = [n for n in archive.namelist() if not n.startswith("/")]
            tops = {n.split("/")[0] for n in names}
            if len(tops) != 1:
                raise UpdateFailed("That download isn't shaped like this "
                                   "project, so nothing was changed.")
            archive.extractall(into)
            # zipfile drops the executable bit on the way out, which would leave
            # the Mac launcher un-double-clickable. git archive records the real
            # mode in the high half of external_attr, so put it back.
            for entry in archive.infolist():
                mode = (entry.external_attr >> 16) & 0o777
                if mode and not entry.is_dir():
                    try:
                        os.chmod(into / entry.filename, mode)
                    except OSError:
                        pass  # Windows, where none of this applies
    except zipfile.BadZipFile:
        raise UpdateFailed("The download was incomplete or damaged, so nothing "
                           "was changed. Try again.")
    return into / tops.pop()


def verify(tree):
    """Refuse to install anything that isn't recognisably this project.

    A download that got truncated, or a login page from a captive hotel wifi
    served with a 200, both arrive looking like success. Nothing is overwritten
    until the new copy has the pieces it's supposed to have.
    """
    for required in ("src/fb_marketplace_sweep.py", "src/version.py",
                     "requirements.txt"):
        if not (tree / required).is_file():
            raise UpdateFailed("That download isn't shaped like this project, "
                               "so nothing was changed.")
    found = VERSION_RE.search(
        (tree / "src" / "version.py").read_text(encoding="utf-8"))
    if not found:
        raise UpdateFailed("The new copy doesn't say what version it is, so "
                           "nothing was changed.")
    return found.group(1)


# --- Putting it in place -----------------------------------------------------

def shipped_files(tree):
    """Every file the new version brings, relative to the project folder."""
    out = []
    for path in sorted(tree.rglob("*")):
        relative = path.relative_to(tree)
        if relative.parts[0] in PROTECTED or "__pycache__" in relative.parts:
            continue
        if path.is_file():
            out.append(relative)
    return out


def stale_files(shipped):
    """Code this version has dropped, still sitting in the folder.

    Only under PRUNED, and only files the new copy doesn't have — a module
    deleted upstream shouldn't linger where something might still import it.
    """
    keeping = set(shipped)
    out = []
    for folder in PRUNED:
        if not (ROOT / folder).is_dir():
            continue
        for path in sorted((ROOT / folder).rglob("*")):
            relative = path.relative_to(ROOT)
            if not path.is_file() or "__pycache__" in relative.parts:
                continue
            if path.name == ".DS_Store" or relative in keeping:
                continue
            out.append(relative)
    return out


def _install(source, destination):
    """One file into place, as a rename so it's all-or-nothing.

    Renaming rather than writing over the top matters for the launchers: the
    shell is part-way through reading the very file being replaced, and it holds
    the old one open, so a rename leaves the running script untouched.

    Returns False if the project folder wouldn't take the file. Only the root is
    allowed to fail this way — see apply().
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged = destination.with_name(destination.name + ".new")
    shutil.copy2(source, staged)
    try:
        os.replace(staged, destination)
    except OSError:
        staged.unlink(missing_ok=True)
        if destination.parent == ROOT:
            return False
        raise
    return True


def _put_back(backup, replaced, added):
    """Undo a half-finished update. Runs with another exception already in
    flight, so it reports nothing and raises nothing — there'd be no room to say
    it, and the failure being handled is the one worth hearing about."""
    for relative in replaced:
        kept = backup / relative
        if not kept.exists():
            continue
        try:
            (ROOT / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(kept, ROOT / relative)
        except OSError:
            pass
    for relative in added:
        try:
            (ROOT / relative).unlink(missing_ok=True)
        except OSError:
            pass


def apply(tree):
    """Replace this copy's code with the one in `tree`.

    Downloading and unpacking happen off to one side, where failing costs
    nothing. This is the part that can't be undone by walking away, so the
    files about to be written over are copied aside first and put back if
    anything goes wrong half way — a full disk, a folder gone read-only, a
    laptop lid closed at the wrong moment. Either the update lands or the folder
    is as it was.

    Returns anything the user should know about a file that wouldn't budge.
    """
    shipped = shipped_files(tree)
    stale = stale_files(shipped)
    backup = UPDATE_DIR / "previous"
    shutil.rmtree(backup, ignore_errors=True)

    replaced, added, notes = [], [], []
    try:
        for relative in shipped + stale:
            live = ROOT / relative
            if not live.exists():
                added.append(relative)
                continue
            kept = backup / relative
            kept.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(live, kept)
            replaced.append(relative)

        for relative in shipped:
            if not _install(tree / relative, ROOT / relative):
                # A file in the root that Windows wouldn't let go of, which in
                # practice means a launcher the running window still has open.
                # Not worth abandoning the rest of the update over.
                notes.append(f"“{relative}” is still on the old version; it was "
                             f"in use. It'll update next time.")
        for relative in stale:
            (ROOT / relative).unlink(missing_ok=True)
    except Exception:
        _put_back(backup, replaced, added)
        shutil.rmtree(backup, ignore_errors=True)
        raise

    shutil.rmtree(backup, ignore_errors=True)
    # Compiled copies of modules that no longer exist. Python wouldn't load them
    # anyway, but leaving them makes the folder confusing to read.
    shutil.rmtree(ROOT / "src" / "__pycache__", ignore_errors=True)
    return notes


# --- The whole thing ---------------------------------------------------------

@contextlib.contextmanager
def nothing_else_running():
    """Holds the same lock a sweep takes, for as long as the update runs.

    A sweep that started an hour ago has most of this program loaded already —
    but not all of it. The gallery builder is imported at the very end of a run,
    on purpose, so replacing the files underneath one can leave a single sweep
    straddling two versions: an hour of old code, then a new gallery written
    from it. Pruning could also delete a module that run hasn't reached yet.

    Taking the lock rules that out from both directions. An update won't begin
    while a sweep is going, and a scheduled sweep won't begin in the middle of
    an update — its tick finds the lock held, skips, and comes back later, which
    is exactly what it already does when a manual run is in the way.

    The lock is one per project folder, so a second copy of the app unpacked
    somewhere else is a separate install and none of its business.
    """
    try:
        import scheduling
    except Exception:
        # If the module that runs sweeps won't even import, no sweep is running,
        # and an update is very likely what would fix that.
        yield
        return
    try:
        with scheduling.run_lock("an update"):
            yield
    except scheduling.AlreadyRunning as e:
        raise Busy(still_running(e)) from None


def still_running(error):
    """Why the update didn't start, for someone looking at the window.

    Built from what the lock file says rather than from the exception's own
    sentence, which is worded for a terminal and names a manual run and a
    scheduled one differently. To the person reading this it's a search either
    way, and the only useful detail is how long it's been going.
    """
    started = (getattr(error, "holder", None) or {}).get("started")
    try:
        at = datetime.fromisoformat(started)
    except (TypeError, ValueError):
        at = None
    since = ""
    if at:
        # %-I isn't portable to Windows, so the hour is formatted by hand — the
        # same reason scheduling.py does it this way.
        since = (f" It started at {at.hour % 12 or 12}:{at:%M}"
                 f"{'am' if at.hour < 12 else 'pm'}.")
    return (f"A search is running in this folder, so the update will have to "
            f"wait.{since} Let it finish, then try again.")


# The launcher sets this to the exit code it watches for. Its presence is the
# whole handshake, and naming the number here rather than agreeing one in
# advance means the two scripts can't drift apart on what it is.
RELAUNCH_ENV = "FACEPLACE_RELAUNCH"


def relaunch_code():
    """Exit with this and the launcher starts the app again. None if nobody will.

    A restart is the only way onto the new code, and the launcher is the only
    thing that can do it properly: it re-reads requirements.txt on the way past,
    so a version that needs a library the old one didn't gets it installed. This
    process can't do that for itself — it would have to survive its own
    replacement to do the installing.

    None means nothing is listening, and then the honest thing is to ask the
    person to start the app again themselves. That's an old launcher from before
    this existed, a hand-typed `python src/fb_marketplace_sweep.py`, or the
    scheduler running a sweep with no window and nobody watching.
    """
    code = os.environ.get(RELAUNCH_ENV, "").strip()
    # Zero would mean "exited normally", which no launcher can act on, and
    # anything above 255 doesn't survive the trip back on either platform.
    return int(code) if code.isdigit() and 0 < int(code) < 256 else None


def update_now(restart=None):
    """Download the newest version and install it. The settings window's button.

    `restart` is whether something is going to start the app again once this is
    done, which is the difference between the last sentence being a promise and
    being an instruction. Left alone it works that out from the environment; the
    command line passes False, since nothing is watching what it exits with.

    Always answers with a dict rather than raising, because the answer is going
    straight into the page as a sentence.
    """
    if managed_by_git():
        return {"error": "This folder is a git clone, so it updates with "
                         "git pull rather than from here."}
    if restart is None:
        restart = relaunch_code() is not None
    try:
        with nothing_else_running():
            return _download_and_install(restart)
    except Busy as e:
        return {"error": str(e)}


def _download_and_install(restart):
    try:
        data = fetch(ZIP_URL, DOWNLOAD_TIMEOUT, limit=MAX_ZIP_BYTES)
    except Exception as e:
        return {"error": in_plain_words(e)}
    try:
        with tempfile.TemporaryDirectory() as scratch:
            tree = extract(data, Path(scratch))
            version = verify(tree)
            notes = apply(tree)
    except UpdateFailed as e:
        return {"error": str(e)}
    except OSError as e:
        return {"error": f"The update was put back the way it was, because a "
                         f"file couldn't be written: {e.strerror or e}."}
    save_state(latest=version,
               installed=datetime.now().isoformat(timespec="seconds"))
    # Three endings, because there are three different things about to happen.
    # A note means something didn't go quite to plan and is worth reading, so
    # that one waits to be dismissed instead of vanishing on a timer.
    if not restart:
        ending = "Close this window and start Faceplace Marketbook again to use it."
    elif notes:
        ending = "Choose Restart now to finish."
    else:
        ending = "Restarting to finish — this window closes on its own."
    return {"ok": True, "version": version, "notes": notes, "restart": restart,
            "message": f"Updated to {version}. {ending}"}


def news(offer):
    """What `available` found, as the end of a sentence that began "Checking for
    updates... ". Facts only: what to do about them differs between the terminal
    and the command line, so each of those adds its own.
    """
    version, current = offer.get("version"), offer.get("current")
    if offer.get("why") == "unreachable":
        return ("no answer. Couldn't reach GitHub, and there's no earlier "
                "answer to fall back on, so this launch can't tell either way.")
    # Said after the answer rather than instead of it, because the answer still
    # stands — it's just older than it looks.
    old = "" if offer.get("checked") else (
        " Couldn't reach GitHub just now, so that's what the last launch heard.")
    if offer.get("why") == "ahead":
        return (f"this copy is version {current}, which is ahead of the "
                f"repository ({version}). A version pushed in the last few "
                f"minutes can take that long to show up here.{old}")
    if offer.get("why") == "current":
        return f"up to date. This is version {current}.{old}"
    return f"version {version} is available. This is {current}.{old}"


def announce():
    """Say what the check found, in the terminal, on the way past.

    It reports even when there's no news. A check that runs on every launch and
    only speaks up when it has something to offer leaves no way to tell being up
    to date apart from a check that quietly failed, or one that never ran at all
    — and the difference matters most to the person who has just pushed a new
    version and is waiting to see it appear.
    """
    if managed_by_git():
        # No check at all: apply() would throw away uncommitted work, so a clone
        # is left out of this entirely and should hear why rather than nothing.
        print("\nNot checking for updates: this folder is a git clone, so it "
              "updates with git pull.\n")
        return
    # Printed before the asking, so a connection that's going to time out spends
    # those seconds under a line explaining the wait.
    print("\nChecking for updates... ", end="", flush=True)
    try:
        offer = available()
    except Exception as e:
        # available swallows every network failure by itself, so reaching here
        # means something else went wrong. Still not worth stopping a run over.
        print(f"the check couldn't be made. {in_plain_words(e)}\n")
        return
    print(news(offer))
    if offer.get("show"):
        # Not "the window opening next": this same line goes out on a scheduled
        # run, where no window opens at all.
        print("  The settings window has a button to install it.")
    print()


def ui_hooks():
    """What the settings window needs to offer the update and then do it."""
    return {"update_offer": available,
            "update_now": update_now}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Check for a newer version of Faceplace Marketbook, or "
                    "install one.")
    ap.add_argument("--update", action="store_true",
                    help="install the newest version instead of just reporting it")
    a = ap.parse_args(argv)

    if managed_by_git():
        raise SystemExit("This folder is a git clone — use git pull.")
    if a.update:
        # Nothing is watching this process's exit code, so there's no restart to
        # promise: whoever typed this can start the app again themselves.
        answer = update_now(restart=False)
        raise SystemExit(answer.get("error") or answer["message"])
    # force, so a version set aside in the window is still reported to whoever
    # went looking for it on purpose.
    offer = available(force=True)
    if offer["why"] == "unreachable":
        raise SystemExit(f"Checked for updates... {news(offer)}")
    print(f"Checked for updates... {news(offer)}")
    if offer["show"]:
        print("Run this again with --update to install it.")


if __name__ == "__main__":
    sys.exit(main())
