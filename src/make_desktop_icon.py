#!/usr/bin/env python3
"""
Puts a double-clickable Faceplace Marketbook icon where the user asks for it:
the desktop, the Dock on a Mac, or the Start menu on Windows.

    "Start Faceplace Marketbook (Mac).command" --desktop-icon
    "Start Faceplace Marketbook (Windows).bat" --desktop-icon

is the command-line way in, and the way to replace an icon that's been left
pointing at a folder you've since moved. Ordinarily this is reached from the
settings window, which offers a shortcut on a launch that hasn't one, through
ui_hooks() below.

On a Mac a place is a small app bundle whose only job is to open the launcher in
Terminal; on Windows it's an ordinary shortcut to the .bat file. Either way the
picture is ui/faceplace_marketbook_icon.svg, rendered at every size the system
asks for by the same Chromium the app already downloads — so there's no image
library to install and no pre-baked icon files to keep in step with the drawing.

Running it again just replaces what's there, which is also how you point an icon
at the app's folder after moving it.
"""
import argparse
import html
import json
import os
import platform
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import unquote

import paths

# The folder someone opens, which is where the launchers a shortcut has to point
# at live — not where this file lives. See the note in paths.py.
ROOT = paths.ROOT
SVG_PATH = paths.SVG_PATH
# Remembers "don't ask again" and what's been made, so the settings window knows
# whether to raise the subject. Deleting it just means being asked once more.
STATE_PATH = paths.SHORTCUTS_PATH
APP_NAME = "Faceplace Marketbook"
BLURB = "Search Facebook Marketplace across the country"

# Where a shortcut can go, per system, in the order the window offers them. All
# of them are ticked by default: someone who wants the app quicker to reach
# generally wants it in both places, and unticking one is easier than noticing
# the second was never offered.
PLACES = {"Darwin": ("desktop", "dock"), "Windows": ("desktop", "startmenu")}
PLACE_LABELS = {"desktop": "Desktop", "dock": "Dock", "startmenu": "Start menu"}
PLACE_PHRASES = {"desktop": "on your desktop", "dock": "in your Dock",
                 "startmenu": "in your Start menu"}

# macOS lays icons out on a 1024-pixel grid with the rounded square filling the
# middle 824 of it. Drawing edge to edge instead leaves an icon that looks a
# size too big next to everything else in the Dock.
MAC_GRID, MAC_TILE = 1024, 824
MAC_SIZES = (16, 32, 64, 128, 256, 512, 1024)
WINDOWS_SIZES = (16, 32, 48, 64, 128, 256)

MAC_LAUNCH_SCRIPT = """#!/bin/bash
# Written by make_desktop_icon.py. Change it there, not here.
launcher={launcher}
if [ ! -f "$launcher" ]; then
    osascript -e 'display alert "{name}" message "{missing}" as critical' >/dev/null 2>&1
    exit 1
fi
# Unzipping a folder can strip a file's permission to run.
chmod +x "$launcher" 2>/dev/null
exec open -a Terminal "$launcher"
"""

# No apostrophes or double quotes in here: it is passed through a single-quoted
# shell string into AppleScript, and either one would end the string early.
MAC_MISSING_MESSAGE = (
    "This icon can no longer find the Faceplace Marketbook folder, which has "
    "been moved, renamed or deleted. Move this icon to the Trash, then start "
    "the app from the folder and it will offer you a new one."
)

# The Dock's own preference format. _CFURLStringType 0 means the string is a
# plain path rather than a URL.
DOCK_TILE = ("<dict><key>tile-data</key><dict><key>file-data</key><dict>"
             "<key>_CFURLString</key><string>{path}</string>"
             "<key>_CFURLStringType</key><integer>0</integer>"
             "</dict></dict></dict>")

WINDOWS_SHORTCUT_SCRIPT = """$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
foreach ($known in @({folders})) {{
    $folder = [Environment]::GetFolderPath($known)
    if (-not $folder) {{ throw "Windows gave no location for $known" }}
    $path = Join-Path $folder {filename}
    $link = $shell.CreateShortcut($path)
    $link.TargetPath = {launcher}
    $link.WorkingDirectory = {folder}
    $link.IconLocation = {icon} + ',0'
    $link.Description = {blurb}
    $link.Save()
    Write-Output $path
}}
"""

# PowerShell's name for each place's folder, and the Python-side path to look in
# when checking whether something is already there. Asking PowerShell that
# question too would mean starting it up on every launch just to find nothing.
WINDOWS_FOLDERS = {"desktop": "Desktop", "startmenu": "Programs"}


# --- The drawing -------------------------------------------------------------

def artwork():
    """The drawing's <svg> attributes and its contents, kept apart so it can be
    re-wrapped at whatever size and padding each system wants. Only the width
    and height are dropped: the rest has to come along, because settings on that
    tag — fill="none" above all — are what the shapes inside inherit."""
    if not SVG_PATH.exists():
        raise SystemExit(f"Can't find the drawing at {SVG_PATH}.")
    found = re.search(r"<svg\b([^>]*)>(.*)</svg>",
                      SVG_PATH.read_text(encoding="utf-8"), re.DOTALL)
    if not found:
        raise SystemExit(f"{SVG_PATH.name} doesn't look like an SVG file.")
    attributes = re.sub(r'\s(?:width|height)\s*=\s*"[^"]*"', "", found.group(1))
    return attributes.strip(), found.group(2)


def full_bleed(art, size):
    attributes, body = art
    return f'<svg {attributes} width="{size}" height="{size}">{body}</svg>'


def on_mac_grid(art, size):
    attributes, body = art
    inset = (MAC_GRID - MAC_TILE) // 2
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" '
            f'height="{size}" viewBox="0 0 {MAC_GRID} {MAC_GRID}">'
            f'<svg {attributes} x="{inset}" y="{inset}" width="{MAC_TILE}" '
            f'height="{MAC_TILE}">{body}</svg></svg>')


def render(layout, sizes):
    """One PNG per size, each drawn from the vector at that size rather than
    scaled down from a big one, so the thin strokes survive at 16 pixels."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit(
            "This needs the app's own Python. Start the app with "
            "--desktop-icon, rather than running this file by hand.")
    art = artwork()
    pngs = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        # One roomy page for every size, clipped down to the icon. Setting the
        # viewport to 16 pixels instead runs into the browser's own floor on
        # how small a window may be.
        page = browser.new_page(viewport={"width": MAC_GRID, "height": MAC_GRID})
        for size in sizes:
            page.set_content(f'<body style="margin:0">{layout(art, size)}</body>')
            pngs[size] = page.screenshot(
                omit_background=True,
                clip={"x": 0, "y": 0, "width": size, "height": size})
        browser.close()
    return pngs


def write_icns(pngs, dest):
    """iconutil is the only thing that writes .icns, and it ships with macOS.
    It wants a folder of PNGs under the names below; the retina ones are the
    same file at twice the size."""
    names = {16: ("icon_16x16",), 32: ("icon_16x16@2x", "icon_32x32"),
             64: ("icon_32x32@2x",), 128: ("icon_128x128",),
             256: ("icon_128x128@2x", "icon_256x256"),
             512: ("icon_256x256@2x", "icon_512x512"), 1024: ("icon_512x512@2x",)}
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "icon.iconset"
        iconset.mkdir()
        for size, data in pngs.items():
            for name in names[size]:
                (iconset / f"{name}.png").write_bytes(data)
        done = subprocess.run(["iconutil", "-c", "icns", str(iconset),
                               "-o", str(dest)], capture_output=True, text=True)
    if done.returncode != 0:
        raise SystemExit("macOS wouldn't build the icon file:\n"
                         + (done.stderr.strip() or "no reason given"))


def write_ico(pngs, dest):
    """A .ico is a short index followed by the PNGs themselves. The width and
    height fields are one byte each, so 256 is written as 0."""
    sizes = sorted(pngs)
    index, blobs = b"", b""
    offset = 6 + 16 * len(sizes)
    for size in sizes:
        data = pngs[size]
        index += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32,
                             len(data), offset)
        offset += len(data)
        blobs += data
    dest.write_bytes(struct.pack("<HHH", 0, 1, len(sizes)) + index + blobs)


# --- Where things go ---------------------------------------------------------

def desktop_folder():
    folder = Path.home() / "Desktop"
    return folder if folder.is_dir() else Path.home()


def windows_desktops():
    """Every folder that might be this machine's desktop. OneDrive moves it and
    leaves the old one in place, so both have to be checked."""
    folders = [Path(os.environ.get("USERPROFILE", Path.home())) / "Desktop"]
    for key in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        if os.environ.get(key):
            folders.append(Path(os.environ[key]) / "Desktop")
    return [f for f in folders if f.is_dir()]


def windows_start_menu():
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return (Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")


def places_here():
    """The places this computer can put a shortcut."""
    return PLACES.get(platform.system(), ())


def in_dock():
    """Whether the Dock is already holding a tile for the app.

    The Dock rewrites whatever it's given into its own house style, turning a
    path into a percent-encoded file:// URL, so the text is decoded before being
    searched: looking for the plain name alone would miss a tile that's there
    and quietly add a second one.
    """
    done = subprocess.run(["defaults", "read", "com.apple.dock", "persistent-apps"],
                          capture_output=True, text=True)
    tiles = unquote(done.stdout) if done.returncode == 0 else ""
    return f"{APP_NAME}.app" in tiles


def places_taken():
    """The places that already have one. A hand-made shortcut under some other
    name won't be spotted, which is the right way to be wrong: the worst it
    costs is one offer that gets declined."""
    system = platform.system()
    taken = set()
    if system == "Darwin":
        if (desktop_folder() / f"{APP_NAME}.app").exists():
            taken.add("desktop")
        if in_dock():
            taken.add("dock")
    elif system == "Windows":
        if any((f / f"{APP_NAME}.lnk").exists() for f in windows_desktops()):
            taken.add("desktop")
        start = windows_start_menu()
        if start and (start / f"{APP_NAME}.lnk").exists():
            taken.add("startmenu")
    return taken


# --- What the user has already been asked ------------------------------------

def load_state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(**changes):
    state = load_state()
    state.update(changes)
    try:
        STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass  # a read-only folder is no reason to fail the thing they asked for


def stop_asking():
    save_state(never_ask=True)
    return {"ok": True}


def offer():
    """What the settings window needs to know about shortcuts.

    `places` is everywhere this computer can put one, every one of them ticked,
    and it comes back whether or not the question is being asked: the Email &
    Setup tab carries a button that opens the same panel on demand, so someone
    who said "not now" — or who wants a second copy after moving the folder —
    has a way back that isn't the command line.

    `ask` is whether to put that panel up unprompted, which happens on a launch
    where this machine can make a shortcut, has none already, and hasn't been
    told to drop the subject.
    """
    places = places_here()
    ask = bool(places) and not (load_state().get("never_ask") or places_taken())
    return {
        "ask": ask,
        "places": [{"id": p, "label": PLACE_LABELS[p], "on": True}
                   for p in places],
    }


# --- Making them -------------------------------------------------------------

def launcher_path(name):
    launcher = ROOT / name
    if not launcher.exists():
        raise SystemExit(f'Can\'t find "{name}" in {ROOT}. A shortcut opens a '
                         f"launcher, so one has to be there to point at.")
    return launcher


def shell_quote(value):
    return "'" + str(value).replace("'", "'\\''") + "'"


def powershell_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def build_mac_app(folder, pngs, launcher):
    """A bundle with a shell script for an executable. Unsigned, but nothing
    here is a binary macOS could refuse to run, and a bundle built on the
    machine it runs on is never quarantined, so it opens without a warning."""
    folder.mkdir(parents=True, exist_ok=True)
    app = folder / f"{APP_NAME}.app"
    if app.exists():
        shutil.rmtree(app)
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    (contents / "Resources").mkdir(parents=True)

    write_icns(pngs, contents / "Resources" / "icon.icns")
    with (contents / "Info.plist").open("wb") as f:
        plistlib.dump({
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleIdentifier": "com.faceplace.marketbook.desktop",
            "CFBundleExecutable": "start",
            "CFBundleIconFile": "icon",
            "CFBundlePackageType": "APPL",
            "CFBundleSignature": "????",
            "CFBundleShortVersionString": "1.0",
            "CFBundleVersion": "1",
            "LSMinimumSystemVersion": "10.13",
            "NSHighResolutionCapable": True,
        }, f)
    (contents / "PkgInfo").write_text("APPL????", encoding="ascii")

    start = contents / "MacOS" / "start"
    start.write_text(MAC_LAUNCH_SCRIPT.format(launcher=shell_quote(launcher),
                                              name=APP_NAME,
                                              missing=MAC_MISSING_MESSAGE),
                     encoding="utf-8")
    start.chmod(0o755)

    # Finder remembers the icon it saw for a bundle. Touching the folder and
    # re-registering it makes a replaced icon appear without logging out.
    os.utime(app, None)
    subprocess.run(
        ["/System/Library/Frameworks/CoreServices.framework/Frameworks/"
         "LaunchServices.framework/Support/lsregister", "-f", str(app)],
        capture_output=True, check=False)
    return app


def add_to_dock(app):
    """Adds a tile and restarts the Dock, which only reads its list at startup.
    It comes straight back, and open windows are untouched.

    Returns whether the tile was still there afterwards. The Dock writes its own
    state back out as it goes down, so a tile added underneath it can be dropped
    again a second later — worth confirming rather than assuming, since this is
    Apple's furniture and not a documented interface.
    """
    if in_dock():
        return True
    tile = DOCK_TILE.format(path=html.escape(str(app)))
    subprocess.run(["defaults", "write", "com.apple.dock", "persistent-apps",
                    "-array-add", tile], capture_output=True, check=True)
    subprocess.run(["killall", "Dock"], capture_output=True, check=False)
    time.sleep(2)
    return in_dock()


def add_mac(place_ids):
    launcher = launcher_path("Start Faceplace Marketbook (Mac).command")
    launcher.chmod(launcher.stat().st_mode | 0o111)
    pngs = render(on_mac_grid, MAC_SIZES)
    made, trouble = {}, []
    if "desktop" in place_ids:
        made["desktop"] = build_mac_app(desktop_folder(), pngs, launcher)
    if "dock" in place_ids:
        # The Dock holds a path, so it needs one that will keep working after
        # someone tidies their desktop. Their own Applications folder is the
        # place for that, and it needs no administrator to write to.
        app = build_mac_app(Path.home() / "Applications", pngs, launcher)
        if add_to_dock(app):
            made["dock"] = app
        else:
            trouble.append(
                "macOS didn't keep the Dock entry, which it sometimes won't. "
                f"The app itself is in your Applications folder — open that and "
                f"drag {APP_NAME} onto the Dock to finish the job.")
    return made, trouble


def add_windows(place_ids):
    launcher = launcher_path("Start Faceplace Marketbook (Windows).bat")
    pngs = render(full_bleed, WINDOWS_SIZES)
    # The shortcuts point at this file rather than carrying a copy of the
    # picture, so it has to live somewhere permanent. It's generated, so it goes
    # with the other generated things rather than beside the drawing.
    icon = paths.ICO_PATH
    icon.parent.mkdir(parents=True, exist_ok=True)
    write_ico(pngs, icon)

    wanted = [p for p in places_here() if p in place_ids]
    # A .lnk is a binary Windows format. The shell object that writes them is
    # reachable from PowerShell, which is on every Windows 10 and 11 machine.
    script = WINDOWS_SHORTCUT_SCRIPT.format(
        folders=", ".join(powershell_quote(WINDOWS_FOLDERS[p]) for p in wanted),
        filename=powershell_quote(f"{APP_NAME}.lnk"),
        launcher=powershell_quote(launcher),
        folder=powershell_quote(ROOT),
        icon=powershell_quote(icon),
        blurb=powershell_quote(BLURB))
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "shortcut.ps1"
        path.write_text(script, encoding="utf-8")
        command = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path)]
        try:
            done = subprocess.run(["powershell"] + command, capture_output=True,
                                  text=True)
        except FileNotFoundError:
            # A PATH that has lost its Windows defaults. The real location is
            # fixed on every version that matters.
            full = (Path(os.environ.get("SystemRoot", r"C:\Windows"))
                    / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
            done = subprocess.run([str(full)] + command, capture_output=True,
                                  text=True)
    if done.returncode != 0:
        raise SystemExit("Windows wouldn't create the shortcut:\n"
                         + (done.stderr.strip() or "no reason given"))
    written = [line.strip() for line in done.stdout.splitlines() if line.strip()]
    return dict(zip(wanted, [Path(p) for p in written])), []


def add(place_ids):
    """Makes the shortcuts asked for.

    Returns ({place id: where it went}, [anything the user should know]). The
    two aren't exclusive: on a Mac the desktop icon can land while the Dock
    declines its tile.
    """
    places = places_here()
    if not places:
        raise SystemExit("This only knows how to make shortcuts on Mac and "
                         "Windows.")
    wanted = [p for p in places if p in set(place_ids)]
    if not wanted:
        raise SystemExit(f"Nothing to add. Pick from: {', '.join(places)}.")
    made, trouble = (add_mac(wanted) if platform.system() == "Darwin"
                     else add_windows(wanted))
    known = dict(load_state().get("added") or {})
    known.update({k: str(v) for k, v in made.items()})
    save_state(added=known)
    return made, trouble


def sentence(made):
    """'on your desktop and in your Dock', for a message to a person."""
    phrases = [PLACE_PHRASES[p] for p in made]
    if len(phrases) > 1:
        phrases = [", ".join(phrases[:-1]), phrases[-1]]
    return " and ".join(phrases)


def summary(made, trouble):
    """One paragraph covering both what happened and what didn't."""
    said = [f"Done. Your {APP_NAME} icon is {sentence(made)}."] if made else []
    return " ".join(said + list(trouble))


# --- Entry points ------------------------------------------------------------

def ui_hooks():
    """What the settings window needs to offer a shortcut on a launch that
    hasn't one yet."""
    return {"shortcut_offer": offer,
            "add_shortcut": add_from_ui,
            "shortcut_never": stop_asking}


def add_from_ui(place_ids, never=False):
    """Adds shortcuts on behalf of the settings window.

    That window is itself a Playwright browser, and drawing the icon needs a
    Chromium of its own, which can't be started from inside the callback of
    another one. So the work goes to a separate process — the same one the Add
    to Desktop files run.
    """
    ids = [p for p in places_here() if p in set(place_ids or ())]
    if not ids:
        return {"error": "Nothing was ticked, so nothing was added."}
    done = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()),
         "--places", ",".join(ids), "--json"],
        capture_output=True, text=True, cwd=str(ROOT))
    answer = {}
    for line in reversed(done.stdout.splitlines()):
        try:
            answer = json.loads(line)
            break
        except ValueError:
            continue
    if not answer:
        return {"error": (done.stderr.strip() or done.stdout.strip()
                          or "The icon builder stopped without saying why.")}
    if answer.get("error"):
        return answer
    # They've made their choice; asking again later — after they've perhaps
    # tidied the icon away on purpose — would just be nagging.
    if never or answer.get("added"):
        stop_asking()
    return answer


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Put a double-clickable Faceplace Marketbook icon on the "
                    "desktop, in the Dock, or in the Start menu.")
    ap.add_argument("--places", default="desktop",
                    help="comma-separated: desktop, dock (Mac), startmenu "
                         "(Windows). Default: desktop")
    ap.add_argument("--json", action="store_true",
                    help="report the result as JSON, for the settings window")
    a = ap.parse_args(argv)
    wanted = [p.strip() for p in a.places.split(",") if p.strip()]

    if a.json:
        try:
            made, trouble = add(wanted)
        except SystemExit as e:
            print(json.dumps({"error": str(e)}))
            return
        except Exception as e:
            print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
            return
        print(json.dumps({"added": list(made), "ok": not trouble,
                          "where": {k: str(v) for k, v in made.items()},
                          "message": summary(made, trouble)}))
        return

    print("Drawing the icon at every size it's needed in...")
    made, trouble = add(wanted)
    print("")
    print(summary(made, trouble))
    for place in made:
        print(f"  {made[place]}")
    print("")
    print("Double-click it to start a search. To get rid of it, move it to the "
          "Trash\nor delete it — that removes the icon and nothing else.")


if __name__ == "__main__":
    main()
