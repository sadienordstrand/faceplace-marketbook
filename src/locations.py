"""
locations.py

The cities a sweep searches: the twelve the app ships with, the ones you add
yourself, and the parsing that turns a pasted Marketplace URL into the segment
Facebook wants.

Two files, deliberately. locations.json ships with the app and is only ever
read. my_locations.json holds your own cities and is the only one written, so
adding a city is never a change to a tracked file.
"""
import json
import re
from pathlib import Path
from urllib.parse import unquote

import paths

LOC_CACHE = paths.LOC_CACHE
USER_LOC_CACHE = paths.USER_LOC_CACHE

SEG_RE = re.compile(r"/marketplace/([^/]+)/search", re.I)


# Segments that appear right after /marketplace/ but name a feature rather than
# a place, so pasting one of those URLs is a mistake worth catching early.
NOT_A_PLACE = {"item", "you", "category", "categories", "notifications", "saved",
               "selling", "buying", "inbox", "profile", "create", "learn-more",
               "search", "marketplace", "groups"}


def parse_location(text):
    """Pull the location segment out of whatever the user pasted.

    Facebook identifies a Marketplace city by the segment after /marketplace/ —
    usually a name like `dallas`, sometimes a numeric id. We accept a full URL
    from the address bar (the realistic case) or a bare segment, and reject the
    feature URLs people grab by accident."""
    text = (text or "").strip()
    if not text:
        return None, "Paste a Marketplace link, or type the city's slug."
    if "/" in text or "http" in text.lower():
        m = SEG_RE.search(text) or re.search(r"/marketplace/([^/?#]+)", text, re.I)
        if not m:
            return None, ("That link has no /marketplace/<city> in it. Open "
                          "Marketplace, switch to the city, and copy the URL.")
        seg = m.group(1)
    else:
        seg = text
    seg = unquote(seg).strip().strip("/").lower()
    if not seg or not re.fullmatch(r"[a-z0-9._-]+", seg):
        return None, f"'{seg}' doesn't look like a city slug."
    if seg in NOT_A_PLACE:
        return None, (f"'{seg}' is a Facebook page, not a city. Switch "
                      "Marketplace to the city first, then copy the URL.")
    return seg, None


# The cities the tool ships with, as a repair net for locations.json. Spaced so a
# 500-mile radius around each one tiles the continental US, which is why they
# can't be deleted: removing one puts a hole in that tiling that nothing in the
# interface would show you afterwards. Leave a city's box unchecked to skip it.
BUILTIN_LOCATIONS = {
    "Medford, OR": "108173265878171",
    "Sacramento, CA": "sac",
    "Boise, ID": "boise",
    "Phoenix, AZ": "phoenix",
    "Albuquerque, NM": "albuquerque",
    "Bismarck, ND": "105540246145383",
    "Dallas, TX": "dallas",
    "Des Moines, IA": "desmoines",
    "Minneapolis, MN": "minneapolis",
    "Tallahassee, FL": "107903159238479",
    "Pittsburgh, PA": "pittsburgh",
    "Boston, MA": "boston",
}


# A label sits in a fixed-width tile in the settings window, three to a row, and
# an auto-named city is one unbroken run of characters — a slug or a 15-digit id —
# with nowhere to wrap. 20 is what a tile holds at the window's default width,
# which leaves real names like "Colorado Springs" and a bare id intact and cuts
# only the ones that would deform the grid. It's also the label that goes in the
# emails and the CSV, which is the other reason not to keep the whole slug.
# The ellipsis in settings.css is the backstop for names typed by hand.
LABEL_MAX = 20


def _fit(name, suffix):
    room = LABEL_MAX - len(suffix)
    if len(name) <= room:
        return name + suffix
    return name[:room - 1].rstrip() + "…" + suffix


def auto_label(seg, taken=()):
    """The name a city gets when the name box is left blank.

    Two segments can be identical up to the cut. Since nobody chose this name,
    a clash is ours to settle — numbering it is better than refusing a city the
    user asked for over a name they never typed."""
    name = f"loc-{seg}" if seg.isdigit() else seg.replace("-", " ").title()
    label, n = _fit(name, ""), 1
    while label in taken:
        n += 1
        label = _fit(name, f" {n}")
    return label


def read_locations_file(path):
    """`label -> segment` from one of the two location files, or {} if it isn't
    there or isn't readable. A missing or mangled file is a reason to fall back,
    never a reason to stop."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if k and v}


def base_locations():
    """The shipped cities.

    Which cities ship is decided here in code, because that's the coverage
    guarantee and a file that anything can edit is a poor place to keep it.
    locations.json is the readable copy of the same list, and it's read for
    segments only: it can correct one Facebook has changed, a name it loses comes
    back, and a name it adds is ignored. Your own cities go in my_locations.json,
    via the settings window."""
    from_file = read_locations_file(LOC_CACHE)
    return {label: from_file.get(label, seg)
            for label, seg in BUILTIN_LOCATIONS.items()}


def is_builtin(label):
    return label in BUILTIN_LOCATIONS


def load_locations():
    """Every city available to a search: the shipped ones, then your own."""
    mine = read_locations_file(USER_LOC_CACHE)
    base = base_locations()
    return {**base, **{k: v for k, v in mine.items() if k not in base}}


def write_own_locations(mapping):
    # Written to a temp file first: a crash mid-write would corrupt the file,
    # and read_locations_file treats a corrupt file as empty — which would
    # silently drop every city the user ever added.
    tmp = USER_LOC_CACHE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(mapping, indent=2), encoding="utf-8")
    tmp.replace(USER_LOC_CACHE)


def add_location(label, text):
    """Append a city to my_locations.json. Returns (locations, error)."""
    label = (label or "").strip()
    seg, err = parse_location(text)
    if err:
        return None, err
    locs = load_locations()
    label = label or auto_label(seg, locs)
    if label in locs:
        return None, f"You already have a city called '{label}'."
    if seg in locs.values():
        dup = next(k for k, v in locs.items() if v == seg)
        return None, f"That's the same place as '{dup}'."
    write_own_locations({**read_locations_file(USER_LOC_CACHE), label: seg})
    return load_locations(), None


def remove_location(label):
    """Returns (locations, error). Only cities you added yourself can go: the
    shipped ones are spaced to cover the country, so a mis-click shouldn't be able
    to quietly shrink the area being searched."""
    if is_builtin(label):
        return load_locations(), (
            f"'{label}' is one of the cities this tool ships with, and they're "
            f"spaced to cover the country, so it can't be removed. Leave its box "
            f"unchecked to skip it.")
    mine = read_locations_file(USER_LOC_CACHE)
    if label not in mine:
        return load_locations(), f"There's no city called '{label}'."
    mine.pop(label)
    write_own_locations(mine)
    return load_locations(), None


# ---------- import pasted URLs ----------
def import_urls(path):
    """Bulk-add your own cities from a text file of pasted Marketplace URLs.

    Replaces my_locations.json, not the shipped list, so this can't cost you a
    city the coverage depends on."""
    locs = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "http" in line and "," in line[: line.index("http")]:
            label, url = line[: line.index("http")].rstrip(", ").strip(), line[line.index("http"):]
        else:
            label, url = None, line
        m = SEG_RE.search(url)
        if not m:
            print(f"  [skip] no /marketplace/<seg>/search in: {line[:80]}")
            continue
        seg = m.group(1)
        locs[label or auto_label(seg, locs)] = seg
    dupes = [k for k in locs if k in BUILTIN_LOCATIONS]
    for k in dupes:
        locs.pop(k)
    write_own_locations(locs)
    if dupes:
        print(f"  [skip] already shipped with the app: {', '.join(dupes)}")
    print(f"Imported {len(locs)} of your own locations -> {USER_LOC_CACHE.name}. "
          f"The {len(BUILTIN_LOCATIONS)} that come with the app are untouched.")
