#!/bin/bash
# Faceplace Marketbook launcher for macOS. Double-click it.
#
# First run installs what it needs into a .venv folder next to this file and
# downloads the browser it drives. Later runs skip straight to the search
# window. Nothing is installed outside this folder.

# Work in this file's folder. Done with shell expansion rather than `dirname` so
# it can't depend on anything outside bash itself.
here="${0%/*}"
[ -z "$here" ] || [ "$here" = "$0" ] && here="."
cd "$here" || exit 1

# Keeps pip's "a new release is available" advice out of a window aimed at
# someone who doesn't need to hear it.
export PIP_DISABLE_PIP_VERSION_CHECK=1

MIN_PY="3.9"
say() { printf '%s\n' "$1"; }
stop() {
    say ""
    say "$1"
    say ""
    read -r -p "Press Return to close this window. " _
    exit 1
}

# Ask Terminal to close the window this script is running in.
#
# Exiting isn't enough on its own. Terminal decides for itself what to do with a
# window whose shell has finished, and out of the box it decides to keep it,
# leaving "[Process completed]" behind — so quitting the app five times leaves
# five dead windows to tidy up by hand. That setting belongs to whoever owns the
# Mac and isn't ours to change, so the window is closed by asking instead.
#
# The timing is the whole difficulty. Terminal refuses to close a window quietly
# while anything is still running in it — it puts up "do you want to terminate
# running processes in this window?" and waits to be answered, which is worse
# than the window it was meant to save you closing. And at the moment this script
# asks, plenty is still running in it: this script, the osascript doing the
# asking, and whatever of Chromium hasn't finished shutting down yet.
#
# So the asking is handed to a process that outlives this one. It detaches into a
# session of its own, which is what takes it off this terminal and out of the
# count Terminal is about to make. It waits for the window to empty out, and only
# then asks. By then there is nothing left to be warned about, and the window
# goes without a word.
#
# Only ever under Terminal, which is what a double-click opens. Elsewhere — iTerm,
# an ssh session, a shell inside an editor — talking to Terminal would be one
# program driving another, and macOS puts up a permission dialog for that. Under
# Terminal it's Terminal being asked about itself, so there's nothing to permit.
close_this_window() {
    [ "$TERM_PROGRAM" = "Apple_Terminal" ] || return 0
    command -v osascript >/dev/null 2>&1 || return 0
    [ -x "$VPY" ] || return 0
    mine=$(tty) || return 0

    # Which window is ours, found by the terminal device it's showing, because
    # that's the only thing telling it apart from the other Terminal windows
    # someone has open. Those must be left alone.
    #
    # Only when it holds nothing but us. Terminal can close a window but has no
    # way to close a single tab, and a launch that landed in a tab beside other
    # work must not take that work down with it — a dead tab left behind is a far
    # smaller thing than a search in the next tab being killed off.
    #
    # Each window is tried separately: an open Inspector is a window too, and has
    # no tabs to ask about, so reaching it would otherwise abandon the whole hunt.
    window=$(osascript 2>/dev/null <<APPLESCRIPT
set found to "0"
tell application "Terminal"
    repeat with k from 1 to (count of windows)
        try
            set panes to tabs of window k
            if (count of panes) is 1 and (tty of item 1 of panes) is "$mine" then
                set found to (id of window k) as text
            end if
        end try
    end repeat
end tell
return found
APPLESCRIPT
)
    [ -n "$window" ] && [ "$window" != "0" ] || return 0

    # nohup, and not a bare &: the whole point of this process is to still be
    # there after the window's shell has gone, and the hangup that goes out when
    # it does would otherwise reach it before it had ignored anything.
    nohup "$VPY" - "${mine#/dev/}" "$window" >/dev/null 2>&1 <<'PYTHON' &
import os
import subprocess
import sys
import time

terminal, window = sys.argv[1], sys.argv[2]

# A session of our own, so this process has no controlling terminal and so isn't
# one of the ones Terminal is about to count. The fork is what makes that
# possible: setsid only works on a process that doesn't already lead a group.
if os.fork():
    os._exit(0)
os.setsid()


def still_working():
    """Anything at all left on that terminal, ours or Chromium's."""
    found = subprocess.run(["ps", "-t", terminal, "-o", "pid="],
                           capture_output=True, text=True)
    return bool(found.stdout.strip())


# Half a minute is far longer than a shell takes to exit and a browser takes to
# let go. Running out of patience closes the window anyway: by then whatever is
# holding the terminal isn't going to finish, and Terminal will ask about it,
# which is no worse than the window being left open for good.
for _ in range(150):
    if not still_working():
        break
    time.sleep(0.2)

subprocess.run(
    ["osascript", "-e", 'tell application "Terminal" to close '
     '(every window whose id is %s) saving no' % window],
    capture_output=True)
PYTHON
    return 0
}

# --- 1. Find a usable Python -------------------------------------------------
# Newest first. The bare "python3" on a Mac without developer tools installed
# is a stub that fails this check, which is what we want.
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1 &&
       "$c" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
        PY="$c"
        break
    fi
done

if [ -z "$PY" ]; then
    say ""
    say "Faceplace needs Python $MIN_PY or newer, and it isn't installed yet."
    say ""
    say "  1. Open  https://www.python.org/downloads/macos/"
    say "  2. Download the latest 'macOS 64-bit universal2 installer'."
    say "  3. Open the downloaded file and click through the installer."
    say "  4. Double-click this Start Faceplace Marketbook file again."
    stop "That's a one-time install. Nothing else to set up."
fi

# --- 2. Create the private Python folder -------------------------------------
VPY=".venv/bin/python"
if [ ! -x "$VPY" ]; then
    say "First-time setup. This takes a minute or two..."
    rm -rf .venv
    "$PY" -m venv .venv ||
        stop "Could not create the .venv folder. Is this folder read-only?"
fi

# --- 3 to 5. Install what's needed, then go, and again after an update -------
# The app can replace its own files while it's running. When it does, it exits
# with RELAUNCH instead of finishing, and everything from here runs a second
# time: the new version is only actually loaded by starting Python again, and it
# may want a library the old requirements.txt never listed.
#
# Safe to have the loop here even though this file is one of the ones an update
# replaces. Bash has already read this whole loop into memory, and the new file
# arrives as a rename, which leaves the copy bash is reading untouched.
RELAUNCH=75

# And when the app is quit by closing its window, there is nothing in this
# terminal worth reading, so it goes without being dismissed by hand and without
# being left on screen. Quitting the app should not leave a window behind to be
# got rid of as well.
CLOSED=76

while :; do
    # --- 3. Install dependencies, but only when they change ------------------
    # The stamp file is a copy of requirements.txt, so editing that file is what
    # triggers a reinstall.
    if ! cmp -s requirements.txt .venv/.installed; then
        say "Installing the browser automation library..."
        "$VPY" -m pip install --quiet --upgrade pip >/dev/null 2>&1
        "$VPY" -m pip install --quiet -r requirements.txt ||
            stop "Install failed. Check your internet connection and try again."
        cp requirements.txt .venv/.installed
        # A new Playwright pins a new Chromium build, so re-check the browser too.
        rm -f .venv/.browser-installed
    fi

    # --- 4. Download the browser Playwright drives ---------------------------
    if [ ! -f .venv/.browser-installed ]; then
        say "Downloading the browser it drives (about 150 MB, one time only)..."
        "$VPY" -m playwright install chromium ||
            stop "Browser download failed. Check your internet connection and try again."
        : > .venv/.browser-installed
    fi

    # --- 5. Go ---------------------------------------------------------------
    # PYTHONUTF8 keeps accented characters in listing titles from tripping up
    # output on any machine, whatever its regional settings. FACEPLACE_RELAUNCH
    # is how the app knows a restart is on offer at all — without it, it tells
    # the person to start the app again instead of promising to do it for them.
    PYTHONUTF8=1 FACEPLACE_RELAUNCH="$RELAUNCH" "$VPY" src/fb_marketplace_sweep.py "$@"
    status=$?

    [ "$status" -eq "$RELAUNCH" ] || break
    say ""
    say "Starting again on the new version..."
    say ""
done

if [ "$status" -eq "$CLOSED" ]; then
    close_this_window
    exit 0
fi

say ""
if [ "$status" -ne 0 ]; then
    say "Faceplace exited with an error (code $status). The messages above say why."
fi
read -r -p "Press Return to close this window. " _
exit "$status"
