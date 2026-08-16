"""
marks.py
--------
Which listings you starred and which you hid, for one search.

Kept in the run's own folder as `marks.json`. That location does two useful
things for free: the marks go in the bin with the run they belong to, and a
scheduled search — which rewrites one folder every time it runs — carries its
marks from one run to the next without anything having to migrate them.

The same marks are also written into that run's built galleries, into the
`<script id="marks">` block build_gallery leaves for them. **That** copy is the
one that matters to anybody else. A gallery gets emailed and opened on a machine
with none of this on it, so the stars have to be inside the file or they don't
travel at all. Keeping the file current is why the local server rewrites the
block on every change instead of only at build time.

Nothing here talks to a browser or a socket; `scheduling.py` owns the server
that does, and `build_gallery.py` owns the page. This module is the shared
middle so those two don't have to import each other.
"""
import json
import re
from datetime import datetime
from pathlib import Path

# The address the gallery page calls back on. It lives here rather than in
# scheduling.py because both ends need it: the server binds it, and the page
# has it baked in at build time. Loopback, so nothing ever leaves the machine.
HOST = "127.0.0.1"
PORT = 18741
ENDPOINT = "/_marks"

MARKS_NAME = "marks.json"
GALLERY_NAMES = ("gallery.html", "lightweight_gallery.html")

# Starred first, because a listing can only be one or the other and the first
# verdict seen is the one kept — see clean().
KINDS = ("starred", "hidden")

# A run of a few thousand listings is a big one, and an id is 15-odd digits.
# Both caps exist because this arrives from a web page.
MAX_IDS = 20000
MAX_ID_LEN = 64

# The block build_gallery leaves in the page. Matched rather than parsed: the
# file it sits in can be sixty megabytes of inlined photos, and this is the one
# part of it we ever rewrite.
BLOCK = re.compile(r'(<script id="marks" type="application/json">)'
                   r'(.*?)(</script>)', re.S)


def empty():
    return {kind: [] for kind in KINDS}


def clean(value):
    """A marks payload reduced to what we're willing to store: two lists of
    listing ids, deduplicated, nothing that isn't a short string.

    Every field is checked rather than trusted. This runs on whatever a POST
    from a page put in the body, and it also runs on whatever is in marks.json,
    which is a file on disk a person can edit.

    An id in both lists is kept only in the first, since starring and hiding
    are opposite verdicts and a listing that was both would render as both."""
    out, seen = {}, set()
    src = value if isinstance(value, dict) else {}
    for kind in KINDS:
        raw = src.get(kind)
        ids = []
        for item in (raw if isinstance(raw, list) else [])[:MAX_IDS]:
            if not isinstance(item, str) or not item or len(item) > MAX_ID_LEN:
                continue
            if item in seen:
                continue
            seen.add(item)
            ids.append(item)
        out[kind] = ids
    return out


def read(folder):
    """This run's marks. An unreadable or nonsensical file reads as no marks
    rather than raising: the gallery still opens, it just opens unmarked."""
    try:
        raw = json.loads((Path(folder) / MARKS_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty()
    return clean(raw)


def write(folder, value):
    """Save these marks and hand back the cleaned version that was stored.

    Written to a neighbouring file and moved into place, so a save that dies
    halfway leaves the previous marks rather than half of these ones."""
    marks = clean(value)
    body = dict(marks)
    body["updated"] = datetime.now().isoformat(timespec="seconds")
    path = Path(folder) / MARKS_NAME
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(body, indent=1), encoding="utf-8")
    tmp.replace(path)
    return marks


def block_text(marks):
    """The JSON that goes inside the page's marks block. "</" would end the
    script element early — the same escape build_gallery applies to the
    listings, for the same reason."""
    return json.dumps(clean(marks), ensure_ascii=False).replace("</", "<\\/")


def stamp(html, marks):
    """These marks written into an already-built gallery, everything else in
    the file left exactly as it was. A replacement function rather than a
    string keeps backslashes in the JSON from being read as group references."""
    text = block_text(marks)
    return BLOCK.sub(lambda m: m.group(1) + text + m.group(3), html, count=1)


def restamp(folder, marks):
    """Bring every gallery in a run folder up to date with these marks, and say
    which files changed.

    Failures are passed over on purpose. This runs while someone is clicking
    stars, and a gallery that's open in a preview pane or on a full disk should
    cost them the file being current, not the click."""
    changed = []
    for name in GALLERY_NAMES:
        path = Path(folder) / name
        try:
            html = path.read_text(encoding="utf-8")
        except OSError:
            continue
        fresh = stamp(html, marks)
        if fresh == html:
            continue
        try:
            path.write_text(fresh, encoding="utf-8")
        except OSError:
            continue
        changed.append(path)
    return changed


def run_folder(run_id, runs_dir):
    """A run id from a gallery page back to a folder, or None.

    The id makes the round trip through a web page, so it can't be trusted to
    still be the folder name that was sent out. Same rule the gallery server
    applies to the files it serves: inside runs/, and nowhere else."""
    try:
        base = Path(runs_dir).resolve()
        folder = (base / (run_id or "")).resolve()
    except (TypeError, ValueError, OSError):
        return None
    if folder == base or base not in folder.parents or not folder.is_dir():
        return None
    return folder
