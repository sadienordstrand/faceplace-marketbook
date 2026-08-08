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

# --- 3. Install dependencies, but only when they change ----------------------
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

# --- 4. Download the browser Playwright drives -------------------------------
if [ ! -f .venv/.browser-installed ]; then
    say "Downloading the browser it drives (about 150 MB, one time only)..."
    "$VPY" -m playwright install chromium ||
        stop "Browser download failed. Check your internet connection and try again."
    : > .venv/.browser-installed
fi

# --- 5. Go ------------------------------------------------------------------
# PYTHONUTF8 keeps accented characters in listing titles from tripping up
# output on any machine, whatever its regional settings.
PYTHONUTF8=1 "$VPY" src/fb_marketplace_sweep.py "$@"
status=$?

say ""
if [ "$status" -ne 0 ]; then
    say "Faceplace exited with an error (code $status). The messages above say why."
fi
read -r -p "Press Return to close this window. " _
exit "$status"
