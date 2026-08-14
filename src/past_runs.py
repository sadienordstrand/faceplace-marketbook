#!/usr/bin/env python3
"""
past_runs.py
------------
The Past searches tab: what's in runs/, summarised, and any one of those runs
opened in a browser.

A run folder is whatever a sweep left behind — results.csv, a gallery, and
usually run.json, which is where the numbers on a card come from. Folders
written by a run that was interrupted before its manifest, or by a version that
predates it, still get a card: the CSV alone is enough to say how many listings
are in there, and a run someone can't see is a run they'll start again.

Manual runs are the dated folders directly under runs/. A scheduled search keeps one
folder under runs/saved/ that it rewrites every time, with the previous
manifests and reports in history/ beside it, so its card is the state of the
most recent run and says how many earlier ones are kept.
"""
import csv
import json
import re
import shutil
import webbrowser
from datetime import datetime
from pathlib import Path

import paths
# The Scheduled searches tab says "yesterday at 5:12pm"; this one has to phrase it
# the same way, so the formatting comes from the module that owns it rather than
# being written a second time.
import scheduling

# Rebound as module names because that's what the tests redirect.
RUNS_DIR = paths.RUNS_DIR

SAVED_DIRNAME = "saved"
RESULTS_CSV = "results.csv"
MANIFEST = "run.json"
# The self-contained one first: it carries its photos, so it opens from
# anywhere. The lightweight one only works while it sits beside thumbnails/.
GALLERY_NAMES = ("gallery.html", "lightweight_gallery.html")

# What make_run_dir puts on the end of a folder name, which the card doesn't
# need to repeat: it says when the run happened in words.
DATED = re.compile(r"_\d{2}-\d{2}-\d{4}(?:_\d+)?$")


def _manifest(folder):
    try:
        return json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _csv_listings(folder):
    """How many distinct listings the CSV holds. Only asked when there's no
    manifest to read it off, so the cost of opening the file is rare."""
    try:
        with open(folder / RESULTS_CSV, newline="", encoding="utf-8") as f:
            return len({r.get("item_id") for r in csv.DictReader(f)
                        if r.get("item_id")})
    except (OSError, ValueError):
        return None


def _mtime(path):
    return datetime.fromtimestamp(path.stat().st_mtime).replace(microsecond=0)


def _finished(folder, manifest):
    when = scheduling.parse_iso(manifest.get("finished")
                                or manifest.get("started"))
    if when:
        return when
    # Nothing recorded the time, so the files have to stand in for it. The CSV
    # is written at the end of a run, which makes it the closest thing to one.
    for name in (RESULTS_CSV, MANIFEST):
        if (folder / name).exists():
            return _mtime(folder / name)
    return _mtime(folder)


def gallery_in(folder):
    for name in GALLERY_NAMES:
        if (folder / name).exists():
            return folder / name
    return None


def is_run(folder):
    return folder.is_dir() and any((folder / n).exists()
                                   for n in (RESULTS_CSV, MANIFEST))


def run_folders():
    """Every run folder, paired with whether a scheduled search owns it."""
    if not RUNS_DIR.is_dir():
        return []
    found = []
    try:
        children = sorted(RUNS_DIR.iterdir())
    except OSError:
        return []
    for child in children:
        if not child.is_dir():
            continue
        if child.name == SAVED_DIRNAME:
            found += [(g, True) for g in sorted(child.iterdir()) if is_run(g)]
        elif is_run(child):
            found.append((child, False))
    return found


def summarize(folder, scheduled=False):
    manifest = _manifest(folder)
    when = _finished(folder, manifest)
    listings = manifest.get("unique_listings")
    if listings is None:
        listings = _csv_listings(folder)
    history = folder / "history"
    return {
        "id": folder.relative_to(RUNS_DIR).as_posix(),
        "name": manifest.get("query") or DATED.sub("", folder.name).replace("_", " "),
        "scheduled": scheduled,
        # Sorted on, and never shown: the text beside it is what's read.
        "when": scheduling.iso(when),
        "when_text": scheduling.fmt_when(when),
        "listings": listings,
        "new_listings": (manifest.get("saved_search") or {}).get("new_listings"),
        "cities": len(manifest.get("locations") or ()) or None,
        "duration_text": (scheduling.fmt_dur(manifest["duration_seconds"])
                          if manifest.get("duration_seconds") else None),
        "earlier_runs": (len(list(history.glob("run-*.json")))
                         if history.is_dir() else 0),
    }


def list_runs():
    runs = [summarize(folder, scheduled) for folder, scheduled in run_folders()]
    runs.sort(key=lambda r: r["when"] or "", reverse=True)
    return {"runs": runs}


def folder_for(run_id):
    """A card's id back to a folder, refusing anything that isn't inside runs/.
    The id makes the round trip through the page, so it can't be trusted to
    still be the plain folder name that was sent out."""
    base = RUNS_DIR.resolve()
    try:
        folder = (base / (run_id or "")).resolve()
    except OSError:
        return None
    if folder == base or base not in folder.parents:
        return None
    return folder if folder.is_dir() else None


def open_run(run_id):
    """Open a run's gallery in the everyday browser. Not in the window asking
    for it: that one is Playwright's, and it closes when a search starts.

    Only the ways this can fail are reported. A gallery that opened is a window
    that just appeared in front of whoever clicked, which says so better than a
    line of text back in the settings window could."""
    folder = folder_for(run_id)
    if not folder:
        return {"error": "That folder isn't in runs/ any more. Refresh the list."}
    gallery = gallery_in(folder)
    if not gallery:
        if not (folder / RESULTS_CSV).exists():
            return {"error": "There's nothing in that folder to open."}
        # A run with the gallery step turned off, or one where it failed.
        # Building it now takes a moment and leaves the file there for later.
        try:
            import build_gallery
            gallery = Path(build_gallery.build(folder / RESULTS_CSV, quiet=True))
        except Exception as e:
            return {"error": f"That run has no gallery, and building one now "
                             f"didn't work ({e})."}
    try:
        webbrowser.open(gallery.resolve().as_uri(), new=1)
    except Exception as e:
        return {"error": f"Couldn't open a browser ({e}). The file itself is "
                         f"at {gallery}."}
    return {}


def delete_run(run_id):
    """Throw a run's folder away — results, gallery, photos and all — and hand
    back what's left. There's no undo: the window asks twice before it calls
    this, and that second click is the whole of the guard.

    A scheduled search's folder can go the same way as any other. It holds that
    search's last results and the manifests of the runs before it, but not its
    memory of which listings it has already seen — that lives in the database —
    so the search itself is unharmed and writes the folder again next time."""
    folder = folder_for(run_id)
    # is_run keeps this to the folders that were offered as cards, which rules
    # out runs/saved itself: every search's results in one click would be a
    # careless thing to make possible.
    if not folder or not is_run(folder):
        return {"error": "That folder isn't in runs/ any more. Refresh the list."}
    try:
        shutil.rmtree(folder)
    except OSError as e:
        return {"error": f"Couldn't delete that folder ({e}). Something else "
                         f"may have a file in it open."}
    return list_runs()


def ui_hooks():
    """What the settings window needs for its Past searches tab."""
    return {"list_runs": list_runs, "open_run": open_run,
            "delete_run": delete_run}
