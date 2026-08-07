#!/usr/bin/env python3
"""
fb_marketplace_sweep.py
------------------------
Personal-use, low-frequency Facebook Marketplace tooling. Sweeps saved-city
searches into CSV + SQLite, optionally retrieves each listing's description
from its detail page, and downloads thumbnails locally so they survive URL expiry.

Extraction strategy (most durable first):
  1. Structured JSON: Facebook ships the data it renders from — GraphQL XHR
     responses while scrolling, and <script type="application/json"> blobs in
     the initial page. These carry typed fields (marketplace_listing_title,
     listing_price.formatted_amount, location, photo URI) and survive markup
     churn far better than CSS classes, which are all machine-generated.
  2. DOM fallback: result-card anchors matched by the /marketplace/item/<id>
     href pattern, with line-classification heuristics for price/location/
     mileage/title. Used to establish page order (real results come before
     the "Results from outside your search" divider) and to fill gaps.

README.md is the end-user manual; docs/how-it-works.md covers the internals,
the command-line flags, and the ToS caveats.
"""
import argparse
import contextlib
import csv
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus, unquote

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
PROFILE_DIR = HERE / ".fb_session"
LOC_CACHE = HERE / "locations.json"
# Cities you add yourself. Kept apart from the shipped list, and out of version
# control, so a personal city never shows up as a change to a tracked file.
USER_LOC_CACHE = HERE / "my_locations.json"
OUT_CSV = HERE / "marketplace_results.csv"
# One cumulative database for every run, so the archive of everything ever seen
# lives in one place while each run's snapshot goes in its own folder.
DB_PATH = HERE / "marketplace_results.sqlite"
DEBUG_DIR = HERE / "debug"

# Where downloaded photos go inside a run folder. Runs made before this was
# renamed used "thumbs", so both prefixes count as a local image when reading
# old CSVs and old database rows.
THUMBS_DIRNAME = "thumbnails"
LEGACY_THUMBS_DIRNAMES = ("thumbnails/", "thumbs/")

ITEM_RE = re.compile(r"/marketplace/item/(\d+)")
SEG_RE = re.compile(r"/marketplace/([^/]+)/search", re.I)
PRICE_LINE_RE = re.compile(r"\$[\d,]+(?:\.\d{2})?")
LOC_RE = re.compile(r".+,\s*[A-Z]{2}")
MILES_RE = re.compile(r"[\d.,]+\s*[Kk]?\s*miles", re.I)
BADGE_RE = re.compile(
    r"just listed|pending|sponsored|in stock|out of stock|popular|"
    r"ships to you|see more like this|new listing", re.I)

FIELDS = ["item_id", "title", "price", "url", "image", "listing_location", "miles",
          "description", "source_section", "matches_query", "location_searched",
          "query", "scraped_at", "raw_text"]

# Randomized seconds to wait between detail-page hits while retrieving
# descriptions. This
# is the one knob that trades runtime against how machine-like the traffic
# looks to Facebook. Tuned so the totals land on ~7s and ~9s per listing once
# the fixed page cost below is added.
PACES = {"fast": (1.0, 2.5), "slow": (3.0, 5.0)}
DEFAULT_PACE = "fast"

# Minutes of description retrieval to allow before stopping to ask. 0 or None
# never asks, which is the default: the estimate is printed either way.
DEFAULT_DESCRIPTIONS_BUDGET_MIN = 0

# Fixed per-listing costs on top of the pause, from measured runs: ~3.5s to
# load a detail page and read its payload, plus ~1.5s to fetch and store the
# photo when thumbnails are on. No pace setting can go below these.
PAGE_WORK_SECONDS = 3.5
PHOTO_SAVE_SECONDS = 1.5

# Scroll ceiling per city — a backstop, not the usual stop condition. Facebook
# orders results by relevance, so depth buys steadily weaker matches, and the
# sweep normally quits once matches dry up (see KEEPER_PATIENCE). Not exposed in
# the settings window — change it here if you ever need to reach deeper.
DEFAULT_SCROLLS = 60

# Consecutive scrolls that turn up no listing capable of passing the filters
# before we stop. This, not the ceiling, is what normally ends a city: the
# related-inventory tail keeps producing cards forever, just not relevant ones.
KEEPER_PATIENCE = 3

# One evaluate() call per pass: snapshot every result card plus whether it sits
# after the "Results from outside your search" divider.
#
# Finding that divider is worth some care: when we miss it, every card gets
# labelled "unknown", which passes the section filter, so the whole
# out-of-radius tail survives. Two rules keep it accurate without requiring a
# single text node: candidates must be short (a feed-sized container will match
# the phrase too, but runs to thousands of characters), and we take the deepest
# match by discarding any candidate that contains another one. That tolerates
# Facebook splitting the phrase across nested spans, which the old leaf-node
# check could not.
CARDS_JS = """
() => {
  const rx = /outside\\s+(of\\s+)?your\\s+search|results?\\s+from\\s+(other|nearby)\\s+(cities|areas|locations|places)|more\\s+results?\\s+(from\\s+)?(nearby|further|other)/i;
  const cands = [...document.querySelectorAll('div,span,p,h1,h2,h3,h4,section')]
    .filter(e => {
      const t = e.textContent || '';
      return t.length < 240 && rx.test(t);
    });
  const divider = cands.find(e => !cands.some(o => o !== e && e.contains(o)));
  return [...document.querySelectorAll('a[href*="/marketplace/item/"]')].map(a => {
    const im = a.querySelector('img');
    return {
      href: a.getAttribute('href') || '',
      text: a.innerText || '',
      img: im ? (im.getAttribute('src') || '') : '',
      outside: divider
        ? !!(divider.compareDocumentPosition(a) & Node.DOCUMENT_POSITION_FOLLOWING)
        : false,
      dividerFound: !!divider,
      dividerText: divider ? (divider.textContent || '').trim().slice(0, 90) : '',
    };
  });
}
"""

SCRIPT_JSON_JS = "els => els.map(e => e.textContent || '')"


def human_pause(a=2.0, b=4.5):
    time.sleep(random.uniform(a, b))


def fmt_dur(seconds):
    m, s = divmod(int(round(seconds)), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s" if m else f"{s}s"


def _prevent_sleep():
    """Ask the OS not to sleep. Returns a function that lifts the request, or
    None if this platform has no mechanism we know about."""
    if sys.platform == "darwin":
        # -w ties caffeinate's lifetime to ours, so it can't outlive the run
        # even if we're killed outright.
        proc = subprocess.Popen(["caffeinate", "-ims", "-w", str(os.getpid())])

        def release():
            if proc.poll() is None:
                proc.terminate()
        return release
    if os.name == "nt":
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        import ctypes
        kernel32 = ctypes.windll.kernel32
        if not kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED):
            return None
        return lambda: kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    return None


@contextlib.contextmanager
def keep_awake(announce=True):
    """Keep the machine awake for the duration.

    A full run is hours long, and a laptop that sleeps in the middle drops the
    browser connection, which costs everything not yet written to disk. Failing
    to arrange this is never fatal: the run just proceeds as it always did.
    """
    try:
        release = _prevent_sleep()
    except Exception:
        release = None
    if release and announce:
        print("Keeping this computer awake until the run finishes. Closing a "
              "laptop lid still sleeps it.")
    try:
        yield
    finally:
        if release:
            try:
                release()
            except Exception:
                pass


def launch_context(p, headless=False):
    try:
        return p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR), headless=headless,
            viewport={"width": 1280, "height": 900})
    except Exception as e:
        if "already in use" in str(e) or "existing browser session" in str(e):
            raise SystemExit(
                "A leftover browser window is still using this app's saved "
                "Facebook login.\n"
                "Close any Chromium window this app opened earlier, then try "
                "again.\n"
                + ("If you can't find it, open Task Manager, end any "
                   "'chrome.exe' process whose command line mentions "
                   "fb_session, and try again."
                   if os.name == "nt" else
                   "If you can't find it, run this in Terminal and try again:\n"
                   "  pkill -f 'user-data-dir=.*\\.fb_session'"))
        raise


AUTH_PAGE_RE = re.compile(r"login|checkpoint|two_step_verification|recover", re.I)


def is_logged_in(page):
    """True only for a fully authenticated session. Password/2FA/checkpoint
    pages don't count, and the c_user cookie only exists after full auth."""
    try:
        if page.query_selector('input[name="email"]') or page.query_selector('input[name="pass"]'):
            return False
        if AUTH_PAGE_RE.search(page.url):
            return False
        return any(c["name"] == "c_user"
                   for c in page.context.cookies("https://www.facebook.com"))
    except Exception:
        return False


class SessionExpired(Exception):
    """Raised instead of waiting when nobody is at the keyboard to log in."""


def ensure_logged_in(page, timeout_s=600, unattended=False):
    """Waits (polling, no terminal input needed) until the session is logged in.

    A scheduled run passes a short timeout and unattended=True: there is no one
    to type a password at 5am, so it gives up quickly and raises, which the
    scheduler turns into an email asking you to log in again."""
    page.goto("https://www.facebook.com/", wait_until="domcontentloaded")
    human_pause()
    deadline = time.time() + timeout_s
    warned = False
    while not is_logged_in(page):
        if not warned and not unattended:
            print("\n>> Not logged in. Log in to Facebook BY HAND in the browser "
                  "window (including any two-factor code) — the script continues "
                  "automatically once you're fully in.")
            warned = True
        if time.time() > deadline:
            if unattended:
                raise SessionExpired(
                    "The saved Facebook session is no longer valid.")
            raise SystemExit("Timed out waiting for Facebook login.")
        time.sleep(3)
    if warned:
        print(">> Login detected, continuing.")


def goto_with_retry(page, url, retries=1):
    """Navigate to `url`, retrying transient failures. Returns True on success."""
    for attempt in range(retries + 1):
        try:
            page.goto(url, wait_until="domcontentloaded")
            return True
        except Exception as e:
            if attempt < retries:
                print(f"  navigation retry {attempt + 1}/{retries} after error: {e}")
                human_pause(3.0, 6.0)
            else:
                print(f"  navigation failed after {retries} retries: {e}")
    return False


# ---------- structured-JSON helpers ----------
def iter_json_docs(body):
    """GraphQL responses are sometimes several JSON docs separated by newlines."""
    try:
        yield json.loads(body)
        return
    except ValueError:
        pass
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except ValueError:
            continue


def find_key(obj, key):
    """Yield every value stored under `key` anywhere in a nested JSON object."""
    if isinstance(obj, dict):
        if key in obj:
            yield obj[key]
        for v in obj.values():
            yield from find_key(v, key)
    elif isinstance(obj, list):
        for v in obj:
            yield from find_key(v, key)


def norm_listing(d):
    """Normalize a listing-shaped dict from Facebook's JSON into our fields."""
    iid = d.get("id")
    if not (isinstance(iid, str) and iid.isdigit()):
        return None
    title = d.get("marketplace_listing_title") or d.get("custom_title") or ""
    lp = d.get("listing_price") or {}
    price = lp.get("formatted_amount") or ""
    rg = (d.get("location") or {}).get("reverse_geocode") or {}
    loc = (rg.get("city_page") or {}).get("display_name") or ""
    if not loc and rg.get("city") and rg.get("state"):
        loc = f"{rg['city']}, {rg['state']}"
    photo = d.get("primary_listing_photo") or {}
    img = ((photo.get("image") or {}).get("uri")
           or (photo.get("listing_image") or {}).get("uri") or "")
    miles = ""
    for s in d.get("custom_sub_titles_with_rendering_flags") or []:
        st = (s or {}).get("subtitle", "")
        if "mile" in st.lower():
            miles = st
            break
    if not (title or price):
        return None
    return {"item_id": iid, "title": title, "price": price,
            "listing_location": loc, "image": img, "miles": miles}


def extract_json_listings(bodies, out):
    """Walk JSON payloads for listing-shaped dicts; merge into `out` by id."""
    for body in bodies:
        if "marketplace_listing_title" not in body and "listing_price" not in body:
            continue
        for doc in iter_json_docs(body):
            stack = [doc]
            while stack:
                o = stack.pop()
                if isinstance(o, dict):
                    if "marketplace_listing_title" in o or "listing_price" in o:
                        n = norm_listing(o)
                        if n:
                            cur = out.setdefault(n["item_id"], n)
                            for k, v in n.items():
                                if v and not cur.get(k):
                                    cur[k] = v
                    stack.extend(o.values())
                elif isinstance(o, list):
                    stack.extend(o)


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
    locations.json is the readable copy of the same list, and it's only ever read:
    it can correct a segment Facebook has changed, but a name it adds is treated
    as a city of yours, not a new shipped one, and a name it loses comes back."""
    from_file = read_locations_file(LOC_CACHE)
    return {label: from_file.get(label, seg)
            for label, seg in BUILTIN_LOCATIONS.items()}


def is_builtin(label):
    return label in BUILTIN_LOCATIONS


def migrate_own_locations():
    """Move cities the user added out of locations.json and into their own file.

    Earlier versions appended to locations.json, which meant a personal city
    showed up as a change to a tracked file. Anything in there that isn't shipped
    must be one of those, so it moves rather than being dropped on the floor."""
    strays = {k: v for k, v in read_locations_file(LOC_CACHE).items()
              if k not in BUILTIN_LOCATIONS}
    if strays:
        write_own_locations(strays)
        print(f"Moved your own cities into {USER_LOC_CACHE.name}: "
              f"{', '.join(strays)}. They work exactly as before; they're just no "
              f"longer mixed in with the ones the app ships with.")
    # locations.json is left alone: it's a tracked file, and rewriting it is the
    # habit this split exists to break. `git checkout locations.json` tidies it.
    return strays


def load_locations():
    """Every city available to a search: the shipped ones, then your own."""
    # Gated on the file existing rather than on it having anything in it. An empty
    # my_locations.json means "you removed them all", and re-reading the old file
    # then would hand back the city you just deleted.
    if not USER_LOC_CACHE.exists():
        migrate_own_locations()
    mine = read_locations_file(USER_LOC_CACHE)
    base = base_locations()
    return {**base, **{k: v for k, v in mine.items() if k not in base}}


def write_own_locations(mapping):
    USER_LOC_CACHE.write_text(json.dumps(mapping, indent=2), encoding="utf-8")


def add_location(label, text):
    """Append a city to my_locations.json. Returns (locations, error)."""
    label = (label or "").strip()
    seg, err = parse_location(text)
    if err:
        return None, err
    locs = load_locations()
    label = label or (seg.replace("-", " ").title() if not seg.isdigit() else f"loc-{seg}")
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
        locs[label or (seg if not seg.isdigit() else f"loc-{seg}")] = seg
    dupes = [k for k in locs if k in BUILTIN_LOCATIONS]
    for k in dupes:
        locs.pop(k)
    write_own_locations(locs)
    if dupes:
        print(f"  [skip] already shipped with the app: {', '.join(dupes)}")
    print(f"Imported {len(locs)} of your own locations -> {USER_LOC_CACHE.name}. "
          f"The {len(BUILTIN_LOCATIONS)} that come with the app are untouched.")


# ---------- sweep ----------
def build_search_url(seg, query, exact, min_price=None, max_price=None):
    """minPrice/maxPrice are honoured server-side (verified against the page's
    own filter_price_* payload) and are in whole dollars. Radius is NOT a URL
    parameter — see read_radius_km."""
    url = f"https://www.facebook.com/marketplace/{seg}/search/?query={quote_plus(query)}"
    url += "&exact=true" if exact else "&exact=false"
    if min_price is not None:
        url += f"&minPrice={min_price}"
    if max_price is not None:
        url += f"&maxPrice={max_price}"
    return url


RADIUS_RE = re.compile(r'filter_radius_km\\?":\\?"?(\d+)')


def city_was_dropped(page, seg):
    """Whether Facebook ignored the city we asked for.

    A slug it doesn't recognise doesn't 404. It redirects to
    /marketplace/category/search and answers from whatever location the account
    is currently set to, so a made-up city returns a full page of real listings
    filed under the wrong name. Every real city — including the numeric ids —
    keeps its segment in the URL, which makes the redirect the signal to watch."""
    try:
        return seg.lower() not in (page.url or "").lower()
    except Exception:
        return False


BUY_LOCATION_RE = re.compile(r'"buy_location":\{"display_name":"([^"]{2,60})"')


def city_shown(page):
    """The location Facebook says it searched, for telling the user what they got
    instead of what they asked for."""
    try:
        m = BUY_LOCATION_RE.search(page.content())
        return m.group(1) if m else ""
    except Exception:
        return ""


def read_radius_km(page):
    """Facebook ships the active search filters in the page. The radius is an
    account-level preference set in the Marketplace UI, not something we can
    pass in the URL, so we read it back and report it.

    Worth watching rather than changing: the saved cities are spaced so that a
    500-mile radius around each one tiles the continental US. If Facebook ever
    resets this to a smaller value, coverage quietly develops holes."""
    try:
        m = RADIUS_RE.search(page.content())
        return int(m.group(1)) if m else None
    except Exception:
        return None


# 805 km, Facebook's maximum, is what the city spacing assumes.
EXPECTED_RADIUS_KM = 805


def describe_radius(km, note=True):
    if not km:
        return ""
    miles = round(km / 1.609)
    warn = ""
    if note and km < EXPECTED_RADIUS_KM:
        warn = ("  <- smaller than the ~500 mi the city spacing assumes; "
                "coverage will have gaps")
    return f"~{miles} mi ({km} km){warn}"


def preflight_pause(page, url, skip=False):
    """Hand the browser to the user before the sweep starts, and report the
    radius while they're looking at it.

    The radius is an account-level setting with a 250-mile default, and a run
    started on that default silently searches a quarter of the area the city
    spacing assumes — the listings it misses never show up as an error. Since
    the login step already requires a human at the keyboard, this is the one
    moment where showing them the number costs nothing.

    Returns the radius in km as of the moment they continued, or None."""
    goto_with_retry(page, url)
    human_pause(3.0, 5.0)
    km = read_radius_km(page)
    if skip:
        print(f"  search radius: {describe_radius(km) or 'unknown'}")
        return km

    print("\n" + "=" * 66)
    print("The browser is ready. Before the sweep starts, in that window:")
    print("\n  1. Close any Facebook popups (notifications, cookie banners).")
    if km and km < EXPECTED_RADIUS_KM:
        print(f"  2. Change the search radius. It's set to "
              f"{describe_radius(km, note=False)},")
        print("     but this needs 500 miles to cover the country. Open the")
        print("     location control in the left sidebar to change it.")
    elif km:
        print(f"  2. Check the search radius. It reads "
              f"{describe_radius(km, note=False)},")
        print("     which is what you want.")
    else:
        print("  2. Check the search radius in the left sidebar. It should be")
        print("     500 miles, not the 250 Facebook starts you on.")
    print("     The radius is an account setting, so you only set it once.")
    print("\n  3. Come back here and press Enter.")
    print("=" * 66)
    try:
        input("\nPress Enter to start the sweep (Ctrl-C to quit)... ")
    except EOFError:
        print("(no terminal to wait on — starting)")
        return km
    except KeyboardInterrupt:
        raise SystemExit("\nStopped before the sweep started. Nothing was saved.")

    after = read_radius_km(page)
    if after and after != km:
        print(f"Radius is now {describe_radius(after, note=False)}.")
    return after or km


def query_tokens(query):
    """Alphabetic words (3+ chars) from the query. Required to match."""
    return [t for t in re.findall(r"[a-z]+", query.lower()) if len(t) >= 3]


def query_numbers(query):
    """Numeric parts like '110'. Highly discriminating when present (761 of
    4,698 'defender' hits had it) but sellers often omit them, so these rank
    listings rather than filter them."""
    return re.findall(r"\d+", query)


def word_hits(token, hay):
    """Match at a word start, so 'defender' also catches 'Defenders' but 'van'
    no longer matches 'advantage'."""
    return re.search(r"\b" + re.escape(token), hay) is not None


def matches_query(tokens, *texts):
    hay = " ".join(t for t in texts if t).lower()
    return all(word_hits(t, hay) for t in tokens)


def squash(s):
    """Strip everything but alphanumerics so one --exclude term covers the
    'Can-Am' / 'Can Am' / 'CANAM' spellings that all appear in real listings."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def is_excluded(r, terms):
    if not terms:
        return False
    hay = squash(f"{r.get('title', '')} {r.get('raw_text', '')}")
    return any(squash(t) in hay for t in terms if t.strip())


def price_number(price):
    """Dollar amount as an int, or None when there's no usable price."""
    m = re.search(r"[\d,]+", (price or "").replace(" ", ""))
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return None


def relevance(r, tokens, numbers):
    """Ranks how likely a listing is the thing you actually searched for, so
    description retrieval spends its time at the top of the list."""
    title = (r.get("title") or "").lower()
    score = 0
    for n in numbers:
        if n in title:
            score += 3
    if tokens and all(word_hits(t, title) for t in tokens):
        score += 2  # every query word in the title, not just the card text
    if r.get("source_section") == "search":
        score += 1
    if price_number(r.get("price")) is not None:
        score += 1
    return score


def parse_card_text(text):
    """Classify each card line; the title is the first line that is neither a
    price, a strikethrough original price, a 'City, ST' location, a mileage,
    nor a UI badge like 'Just listed'."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    price = next((ln for ln in lines if PRICE_LINE_RE.fullmatch(ln)), "")
    if not price and any(ln.lower() == "free" for ln in lines):
        price = "Free"
    loc = next((ln for ln in lines if LOC_RE.fullmatch(ln)), "")
    miles = next((ln for ln in lines if MILES_RE.fullmatch(ln)), "")
    title = next((ln for ln in lines
                  if ln not in (price, loc, miles)
                  and not PRICE_LINE_RE.fullmatch(ln)
                  and not BADGE_RE.fullmatch(ln)), "")
    return title, price, loc, miles, lines


def collect_city(page, max_scrolls, is_keeper=None, patience=4,
                 keeper_patience=KEEPER_PATIENCE, verbose=False):
    """Scroll and snapshot cards incrementally (Facebook recycles off-screen
    DOM nodes on long lists, so waiting until the end loses early cards).

    Scrolling stops on whichever comes first:
      * the out-of-radius divider appears (everything past it gets dropped);
      * `keeper_patience` scrolls in a row produce no card that could survive
        the filters;
      * `patience` scrolls in a row produce no new cards at all;
      * the `max_scrolls` ceiling.

    The keeper test is the important one. Facebook's related-inventory tail is
    effectively bottomless on a broad query, so "no new cards" almost never
    fires and depth just buys junk we pay to fetch. `is_keeper` receives a
    card's on-screen text and should answer generously: a card whose text has
    not rendered yet counts as a keeper so missing data can never cut a scroll
    short.

    Returns {item_id: card_dict}, whether the divider was seen, its text, and a
    stats dict describing where and why scrolling stopped."""
    cards, divider_seen, divider_text = {}, False, ""
    keepers, last_keeper_scroll, scroll_no = 0, 0, 0

    def snapshot():
        nonlocal divider_seen, divider_text, keepers, last_keeper_scroll
        # Facebook re-navigates shortly after load (URL canonicalization),
        # which destroys the JS context; retry once instead of crashing.
        for attempt in range(2):
            try:
                snap = page.evaluate(CARDS_JS)
                break
            except Exception as e:
                if attempt == 0:
                    human_pause(2.0, 3.0)
                else:
                    print(f"  card snapshot failed: {e}")
                    snap = []
        for c in snap:
            m = ITEM_RE.search(c["href"])
            if not m:
                continue
            iid = m.group(1)
            if c["dividerFound"] and not divider_seen:
                divider_seen, divider_text = True, c.get("dividerText", "")
            cur = cards.get(iid)
            if cur is None:
                cards[iid] = c
                if is_keeper is None or is_keeper(c):
                    keepers += 1
                    last_keeper_scroll = scroll_no
                continue
            cur["outside"] = cur["outside"] or c["outside"]
            if len(c["text"]) > len(cur.get("text", "")):
                cur["text"] = c["text"]
            if c["img"] and not cur.get("img"):
                cur["img"] = c["img"]
        return len(cards)

    # A snapshot re-reads every card on the page, so scrolls get steadily more
    # expensive as the grid grows. The marginal cost of the scrolls we skipped
    # is therefore closer to the most recent ones than to the average.
    lap_seconds = []

    def stats(reason):
        recent = lap_seconds[-3:] or [0.0]
        return {"scrolls_used": scroll_no, "scroll_ceiling": max_scrolls,
                "cards": len(cards), "keepers_seen": keepers,
                "last_keeper_scroll": last_keeper_scroll,
                "stop_reason": reason,
                "seconds_per_scroll_recent": round(sum(recent) / len(recent), 1)}

    prev = snapshot()
    prev_keepers = keepers
    stable = dry = 0
    if divider_seen:
        print(f"  divider on first screen ({divider_text!r}); no scrolling needed")
        return cards, divider_seen, divider_text, stats("divider")
    for n in range(1, max_scrolls + 1):
        scroll_no = n
        lap = time.time()
        page.mouse.wheel(0, 5000)
        human_pause(1.5, 3.0)
        cur = snapshot()
        lap_seconds.append(time.time() - lap)
        if verbose:
            print(f"    scroll {n}: +{cur - prev} cards, "
                  f"+{keepers - prev_keepers} matches "
                  f"({cur} cards / {keepers} matches so far)", flush=True)
        if divider_seen:
            print(f"  divider reached after {n} scroll{'s' if n != 1 else ''} "
                  f"({divider_text!r}); stopping scroll")
            return cards, divider_seen, divider_text, stats("divider")
        if is_keeper is not None:
            dry = dry + 1 if keepers == prev_keepers else 0
            if dry >= keeper_patience:
                print(f"  {keeper_patience} scrolls with no new matches after "
                      f"scroll {n}; stopping scroll")
                return cards, divider_seen, divider_text, stats("no new matches")
        stable = stable + 1 if cur <= prev else 0
        if stable >= patience:
            print(f"  no new listings for {patience} scrolls; stopping scroll")
            return cards, divider_seen, divider_text, stats("no new listings")
        prev, prev_keepers = cur, keepers
    print(f"  hit the {max_scrolls}-scroll ceiling")
    return cards, divider_seen, divider_text, stats("scroll ceiling")


def build_rows(cards, divider_seen, json_listings, label, query, tokens):
    """Merge DOM cards with structured JSON, classify section + relevance."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = {}
    for iid, c in cards.items():
        title, price, loc, miles, lines = parse_card_text(c.get("text", ""))
        section = ("outside_search" if c.get("outside")
                   else ("search" if divider_seen else "unknown"))
        rows[iid] = {
            "item_id": iid, "title": title, "price": price,
            "url": f"https://www.facebook.com/marketplace/item/{iid}",
            "image": c.get("img", ""), "listing_location": loc, "miles": miles,
            "source_section": section, "location_searched": label,
            "query": query, "scraped_at": now,
            "raw_text": " | ".join(lines)[:300],
        }
    # Structured JSON wins over text heuristics wherever both exist.
    for iid, j in json_listings.items():
        r = rows.setdefault(iid, {
            "item_id": iid, "title": "", "price": "",
            "url": f"https://www.facebook.com/marketplace/item/{iid}",
            "image": "", "listing_location": "", "miles": "",
            "source_section": "unknown", "location_searched": label,
            "query": query, "scraped_at": now, "raw_text": "",
        })
        for k in ("title", "price", "listing_location", "image", "miles"):
            if j.get(k):
                r[k] = j[k]
    for r in rows.values():
        r["matches_query"] = "yes" if matches_query(
            tokens, r["title"], r["raw_text"]) else "no"
    return rows


def keep_row(r, exclude=(), min_price=None, max_price=None):
    """Returns (keep, reason_it_was_dropped)."""
    if r["source_section"] == "outside_search":
        return False, "outside search"
    if r["matches_query"] != "yes":
        return False, "query words missing"
    if is_excluded(r, exclude):
        return False, "excluded term"
    p = price_number(r.get("price"))
    # A missing price is kept: plenty of real listings say "Free" or omit it,
    # and the price bounds are already applied server-side via the URL.
    if p is not None:
        if min_price is not None and p < min_price:
            return False, "under min price"
        if max_price is not None and p > max_price:
            return False, "over max price"
    return True, ""


def card_may_keep(card, tokens, exclude=(), min_price=None, max_price=None):
    """The in-loop version of keep_row, used only to decide whether a scroll
    was worth doing.

    It sees a raw card before the structured-JSON merge, so it deliberately
    errs toward yes: a card whose text has not rendered, or whose price did not
    parse, counts as a match rather than risk cutting the scroll short. Real
    filtering still happens on the merged rows afterwards, so a generous answer
    here costs a couple of extra scrolls at worst."""
    if card.get("outside"):
        return False
    title, price, _loc, _miles, lines = parse_card_text(card.get("text", ""))
    raw = " | ".join(lines)[:300]
    if not title and not raw:
        return True
    r = {"source_section": "unknown", "title": title, "price": price,
         "raw_text": raw,
         "matches_query": "yes" if matches_query(tokens, title, raw) else "no"}
    ok, _why = keep_row(r, exclude, min_price, max_price)
    return ok


def write_csv(rows, path, fields=FIELDS):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


# Tables that only scheduled saved searches use. Kept apart from `listings` so
# the FIELDS list stays exactly the CSV's columns.
SCHEDULE_SCHEMA = (
    # Per-listing bookkeeping: what we've already paid to fetch, and whether the
    # listing is still up. status is 'live', 'sold' or 'gone'.
    """CREATE TABLE IF NOT EXISTS listing_state (
         item_id TEXT PRIMARY KEY,
         first_seen TEXT, last_seen_in_feed TEXT, last_verified TEXT,
         status TEXT DEFAULT 'live', status_confirmed_at TEXT,
         verify_failures INTEGER DEFAULT 0, description_fetched_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS search_runs (
         run_id INTEGER PRIMARY KEY AUTOINCREMENT,
         search_id TEXT, started TEXT, finished TEXT, duration_seconds REAL,
         new_count INTEGER, total_count INTEGER, removed_count INTEGER,
         status TEXT, error TEXT)""",
    "CREATE INDEX IF NOT EXISTS ix_search_runs_search ON search_runs(search_id, run_id)",
    # Which listings made up a given run's results, which is what "the results in
    # the most recent run" means when deciding what to re-verify.
    """CREATE TABLE IF NOT EXISTS run_items (
         run_id INTEGER, item_id TEXT, is_new INTEGER DEFAULT 0,
         PRIMARY KEY(run_id, item_id))""",
)

# Columns a sweep row can't be trusted to fill in. A sweep only sees search
# cards, so its `description` is always blank and its `image` is always a remote
# URL — letting those overwrite what a detail-page visit already stored would
# throw away the most expensive work the tool does, every single run.
KEEP_IF_BLANK = ("description", "raw_text")


def open_db(db_path):
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE IF NOT EXISTS listings (%s, PRIMARY KEY(item_id))"
                % ",".join(f"{c} TEXT" for c in FIELDS))
    existing = {row[1] for row in con.execute("PRAGMA table_info(listings)")}
    for c in FIELDS:
        if c not in existing:
            con.execute(f"ALTER TABLE listings ADD COLUMN {c} TEXT")
    for stmt in SCHEDULE_SCHEMA:
        con.execute(stmt)
    con.commit()
    return con


def _upsert_sql():
    cols = ",".join(FIELDS)
    ph = ",".join(f":{c}" for c in FIELDS)
    sets = []
    for c in FIELDS:
        if c == "item_id":
            continue
        if c in KEEP_IF_BLANK:
            sets.append(f"{c}=CASE WHEN excluded.{c} <> '' "
                        f"THEN excluded.{c} ELSE listings.{c} END")
        elif c == "image":
            # Order matters, and it cost a wrong answer once. A local path beats a
            # remote URL, because the file is on disk and the URL expires. But a
            # NEWER local path also has to beat an older one: the old one names a
            # file in the run folder that wrote it, and a later run reading this
            # row would point at a photo that isn't there.
            sets.append("image=CASE "
                        "WHEN excluded.image <> '' AND excluded.image NOT LIKE 'http%' "
                        "THEN excluded.image "
                        "WHEN listings.image <> '' AND listings.image NOT LIKE 'http%' "
                        "THEN listings.image "
                        "WHEN excluded.image <> '' THEN excluded.image "
                        "ELSE listings.image END")
        else:
            sets.append(f"{c}=excluded.{c}")
    return (f"INSERT INTO listings ({cols}) VALUES ({ph}) "
            f"ON CONFLICT(item_id) DO UPDATE SET {','.join(sets)}")


UPSERT_SQL = _upsert_sql()


def upsert(con, r):
    con.execute(UPSERT_SQL, {c: r.get(c, "") for c in FIELDS})


# ---------- per-run output folders ----------
def slugify(s):
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_") or "search"


def make_run_dir(query, base=None):
    """runs/<query-slug>_<mm-dd-yyyy>, suffixed _1, _2, ... so a run can never
    overwrite an earlier one."""
    parent = (base or (HERE / "runs"))
    parent.mkdir(parents=True, exist_ok=True)
    stem = f"{slugify(query)}_{datetime.now().strftime('%m-%d-%Y')}"
    d = parent / stem
    n = 0
    while d.exists():
        n += 1
        d = parent / f"{stem}_{n}"
    d.mkdir()
    return d


def reconcile_with_previous(all_rows, prev_by_id, gone_ids, score=None):
    """Fold the last run's results into this one's.

    A listing that didn't turn up in this sweep is kept, because Facebook's
    ranking is not a promise: absence from the feed is not evidence the listing
    is gone. Only the ids in gone_ids, which a check actually confirmed, are
    dropped. Mutates all_rows and returns (new_ids, carried_count)."""
    new_ids = [i for i in all_rows if i not in prev_by_id]
    # A sweep row is a search card, so its description is always blank. Without
    # this, a listing that keeps appearing in the feed looks undescribed every
    # run and gets its detail page fetched again forever — which is the exact
    # cost a scheduled search exists to avoid paying twice.
    for iid, row in all_rows.items():
        old = prev_by_id.get(iid)
        if not old:
            continue
        for field in KEEP_IF_BLANK:
            if not (row.get(field) or "") and (old.get(field) or ""):
                row[field] = old[field]
    carried = 0
    for iid, r in prev_by_id.items():
        if iid in all_rows or iid in gone_ids:
            continue
        if score:
            r["_score"] = score(r)
        all_rows[iid] = r
        carried += 1
    return new_ids, carried


def saved_run_dir(name, base=None):
    """runs/saved/<name-slug>/ — one stable folder per saved search, rewritten in
    place every run. A scheduled search that made a new dated folder every hour
    would bury the results it's meant to surface."""
    d = (base or (HERE / "runs" / "saved")) / slugify(name)
    d.mkdir(parents=True, exist_ok=True)
    return d


def run(query, scrolls, exact, out_csv=None, only=None, keep_all=False,
        debug_dump=False, match=None, limit=None, thumbs_dir=THUMBS_DIRNAME,
        do_descriptions=True, do_thumbs=True, do_gallery=True, pace=DEFAULT_PACE,
        exclude=(), min_price=None, max_price=None, descriptions_budget=DEFAULT_DESCRIPTIONS_BUDGET_MIN,
        assume_yes=False, only_labels=None, open_gallery=True, no_pause=False,
        run_dir=None, previous_rows=None, describe_new_only=False, verifier=None,
        login_wait=None, unattended=False):
    """One pass over everything: sweep every saved city, visit each kept
    listing's detail page at most once for its description and full-size photo,
    save that photo locally while its URL is still fresh, then build the
    gallery — all in a single browser session.

    Output goes to its own runs/<query>_<date>/ folder unless out_csv or run_dir
    overrides it. The SQLite database stays in the project root as the cumulative
    index across every run.

    A scheduled saved search passes the extra arguments: run_dir to write into
    the same folder every time, previous_rows for what the last run found,
    describe_new_only so old listings aren't re-fetched, and verifier to check
    whether the listings that stopped appearing are actually gone. Returns a
    summary dict; interactive callers ignore it."""
    all_locs = load_locations()
    locs = all_locs
    if only_labels is not None:
        locs = {k: v for k, v in locs.items() if k in set(only_labels)}
    elif only:
        locs = {k: v for k, v in locs.items() if only.lower() in k.lower()}
    if not locs:
        avail = ", ".join(all_locs)
        print(f"No matching locations. Available: {avail}")
        return {"status": "error", "error": f"No matching locations (have: {avail})"}
    started = time.time()
    started_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tokens = query_tokens(query)
    numbers = query_numbers(query)
    if run_dir:
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        out_path = run_dir / "results.csv"
    elif out_csv:
        out_path, run_dir = Path(out_csv), None
    else:
        run_dir = make_run_dir(query)
        out_path = run_dir / "results.csv"
    con = open_db(DB_PATH)
    debug_root = (run_dir / "debug") if run_dir else DEBUG_DIR
    if debug_dump:
        debug_root.mkdir(parents=True, exist_ok=True)
    stages = ["sweep"] + (["retrieve descriptions"] if do_descriptions else []) \
        + (["thumbnails"] if do_thumbs else []) + (["gallery"] if do_gallery else [])
    print(f"Plan: {' -> '.join(stages)} for {len(locs)} "
          f"location{'s' if len(locs) != 1 else ''}, query '{query}'"
          + (f", '{pace}' description pacing." if do_descriptions else "."))
    if run_dir:
        print(f"Output folder: {run_dir}")
    if exclude:
        print(f"Excluding: {', '.join(exclude)}")
    if min_price is not None or max_price is not None:
        print(f"Price filter: {min_price if min_price is not None else 'any'} - "
              f"{max_price if max_price is not None else 'any'}")
    all_rows, dropped_total = {}, 0
    city_stats, drop_reasons, radius_km = {}, {}, None
    unknown_cities = []
    prev_by_id = {r["item_id"]: dict(r) for r in (previous_rows or [])
                  if r.get("item_id")}
    new_ids, removed, verified_count = [], [], 0
    with keep_awake(), sync_playwright() as p:
        ctx = launch_context(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        graphql_bodies = []

        def on_response(resp):
            if "/api/graphql" not in resp.url:
                return
            try:
                body = resp.text()
            except Exception:
                return
            if "marketplace" in body:
                graphql_bodies.append(body)

        page.on("response", on_response)
        ensure_logged_in(page, timeout_s=login_wait or 600, unattended=unattended)
        first_seg = next(iter(locs.values()))
        radius_km = preflight_pause(
            page, build_search_url(first_seg, query, exact, min_price, max_price),
            skip=no_pause)
        for label, seg in locs.items():
            url = build_search_url(seg, query, exact, min_price, max_price)
            print(f"\n[{label}] {url}")
            graphql_bodies.clear()
            if not goto_with_retry(page, url):
                continue
            human_pause(3.0, 5.0)
            if city_was_dropped(page, seg):
                # Sweeping it anyway would file another city's listings under
                # this name, which looks like coverage and isn't.
                instead = city_shown(page)
                print(f"  Facebook doesn't recognise this city, so it searched "
                      f"{instead or 'wherever your account is set to'} instead. "
                      f"Skipping it — remove '{label}' and add it again from a "
                      f"Marketplace URL.")
                unknown_cities.append({"label": label, "seg": seg,
                                       "searched_instead": instead})
                continue
            try:
                page.wait_for_selector('a[href*="/marketplace/item/"]', timeout=15000)
            except Exception:
                pass  # zero results or slow load; scroll anyway
            if radius_km is None:
                radius_km = read_radius_km(page)
                if radius_km:
                    print(f"  search radius: {describe_radius(radius_km)}")
            # --keep-all wants the unfiltered tail, so it falls back to the
            # "no new cards at all" stop instead of the keeper-aware one.
            probe = None if keep_all else (
                lambda c: card_may_keep(c, tokens, exclude, min_price, max_price))
            scroll_started = time.time()
            cards, divider_seen, _dtext, cstats = collect_city(
                page, scrolls, probe, verbose=True)
            cstats["scroll_seconds"] = round(time.time() - scroll_started, 1)
            # initial page results are embedded as JSON in script tags
            script_bodies = page.eval_on_selector_all(
                'script[type="application/json"]', SCRIPT_JSON_JS)
            if debug_dump:
                dump = debug_root / f"sweep_{seg}"
                dump.mkdir(parents=True, exist_ok=True)
                for i, b in enumerate(graphql_bodies):
                    (dump / f"graphql_{i:03d}.json").write_text(b, encoding="utf-8")
                (dump / "scripts.json").write_text(json.dumps(script_bodies), encoding="utf-8")
            json_listings = {}
            extract_json_listings(script_bodies, json_listings)
            extract_json_listings(graphql_bodies, json_listings)
            rows = build_rows(cards, divider_seen, json_listings, label, query, tokens)
            kept = {}
            for iid, r in rows.items():
                ok, why = keep_row(r, exclude, min_price, max_price)
                if keep_all or ok:
                    kept[iid] = r
                else:
                    drop_reasons[why] = drop_reasons.get(why, 0) + 1
            dropped_total += len(rows) - len(kept)
            print(f"  {len(cards)} cards in DOM, {len(json_listings)} structured JSON "
                  f"listings, divider {'seen' if divider_seen else 'not seen'}")
            print(f"  kept {len(kept)}, dropped {len(rows) - len(kept)}")
            # Per-scroll accounting, so it is obvious whether the early stop is
            # saving work or cutting off real results.
            skipped = scrolls - cstats["scrolls_used"]
            saved = skipped * cstats["seconds_per_scroll_recent"]
            cstats.update(seconds_saved_estimate=round(saved, 1),
                          kept=len(kept), dropped=len(rows) - len(kept),
                          divider_seen=divider_seen)
            print(f"  {cstats['scrolls_used']} of {scrolls} scrolls in "
                  f"{fmt_dur(cstats['scroll_seconds'])} ({cstats['stop_reason']}); "
                  f"last new match on scroll {cstats['last_keeper_scroll']}; "
                  f"skipped {skipped} scrolls, saving at least {fmt_dur(saved)}")
            city_stats[label] = cstats
            for iid, r in kept.items():
                all_rows.setdefault(iid, r)
                upsert(con, r)
            con.commit()
            human_pause(6.0, 14.0)

        rows = list(all_rows.values())
        for r in rows:
            r["_score"] = relevance(r, tokens, numbers)
        dup_collapsed = sum(s["kept"] for s in city_stats.values()) - len(rows)
        print(f"\nSwept {len(rows)} unique listings ({dropped_total} dropped, "
              f"{dup_collapsed} duplicates across cities collapsed).")
        scroll_used = sum(s.get("scrolls_used", 0) for s in city_stats.values())
        scroll_possible = scrolls * len(city_stats)
        scroll_saved = sum(s.get("seconds_saved_estimate", 0)
                           for s in city_stats.values())
        cards_seen = sum(s.get("cards", 0) for s in city_stats.values())
        if city_stats:
            print(f"  scrolling: {scroll_used} of {scroll_possible} possible "
                  f"scrolls used across {len(city_stats)} cities, "
                  f"{cards_seen} cards examined, at least "
                  f"{fmt_dur(scroll_saved)} of sweeping skipped by stopping "
                  f"early once matches dried up")
        if drop_reasons:
            print("  dropped because: "
                  + ", ".join(f"{k} ({v})" for k, v in
                              sorted(drop_reasons.items(), key=lambda kv: -kv[1])))
        if unknown_cities:
            print("  cities Facebook didn't recognise, so nothing was searched "
                  "for them: "
                  + ", ".join(f"{c['label']} ({c['seg']})" for c in unknown_cities))
        if not keep_all:
            print("  (--keep-all keeps filtered listings, flagged in the "
                  "source_section / matches_query columns, instead of dropping them.)")

        # ---- saved-search bookkeeping ----
        # A listing in this run's feed is proven alive for free. One that has
        # stopped appearing might be gone, or the ranking might simply not have
        # surfaced it this time, so it is only dropped once a check confirms it.
        feed_ids = set(all_rows)
        new_ids = [i for i in all_rows if i not in prev_by_id]
        if prev_by_id:
            print(f"\n{len(new_ids)} of these are new since the last run.")
            missing = [prev_by_id[i] for i in prev_by_id if i not in feed_ids]
            if missing and verifier:
                print(f"Checking {len(missing)} listing"
                      f"{'' if len(missing) == 1 else 's'} that didn't turn up "
                      f"this time...")
                removed, verified_count, auth_failed = verifier(ctx, missing)
                if auth_failed:
                    raise SessionExpired(
                        "The Facebook session expired while checking listings.")
                print(f"  {len(removed)} confirmed sold or taken down, "
                      f"{verified_count - len(removed)} still up.")
            new_ids, carried = reconcile_with_previous(
                all_rows, prev_by_id, {r["item_id"] for r in removed},
                score=lambda r: relevance(r, tokens, numbers))
            if carried:
                print(f"  {carried} kept from previous runs (not in this feed, "
                      f"but not confirmed gone either).")
            rows = list(all_rows.values())

        thumbs_path = Path(thumbs_dir)
        if not thumbs_path.is_absolute():
            thumbs_path = out_path.resolve().parent / thumbs_path

        described, interrupted, described_ids = 0, False, []
        if do_descriptions and rows:
            targets = [r for r in rows
                       if not match or match.lower() in (r.get("title", "").lower())]
            if describe_new_only:
                # The whole point of the schedule: a detail page costs about
                # seven seconds, so only visit listings we've never described.
                fresh = set(new_ids)
                targets = [r for r in targets
                           if r["item_id"] in fresh or not (r.get("description") or "")]
            # Best matches first, so a capped or interrupted run still spends
            # its time on the listings most likely to be what you searched for.
            targets.sort(key=lambda r: (-r["_score"], r.get("title", "")))
            if limit:
                targets = targets[:limit]
            targets = confirm_description_count(targets, pace, descriptions_budget,
                                          assume_yes, do_thumbs)
            if targets:
                # Commit per listing: this stage runs for as long as an hour and
                # is the one people interrupt, so nothing may sit in memory
                # waiting for the stage to finish.
                def save_row(r):
                    upsert(con, r)
                    con.commit()
                finished = retrieve_descriptions(ctx, page, targets,
                                       thumbs_path if do_thumbs else None,
                                       debug_dump, pace, on_row=save_row)
                described = sum(1 for r in targets if r.get("description"))
                described_ids = [r["item_id"] for r in targets if r.get("description")]
                # An interrupt means stop working, not throw away the run: skip
                # the remaining downloads and go straight to the CSV + gallery.
                interrupted = not finished

        if do_thumbs and rows and not interrupted:
            try:
                fetch_thumbs(ctx, rows, thumbs_path)
            except KeyboardInterrupt:
                interrupted = True
                print("\n  Interrupted. Keeping the photos already downloaded.")
            for r in rows:
                upsert(con, r)
            con.commit()
        try:
            ctx.close()
        except Exception:
            pass
    con.close()
    write_csv(rows, out_path)
    print(f"\nWrote {out_path} and {DB_PATH}")
    gallery, gallery_path = None, None
    if do_gallery and rows:
        try:
            import build_gallery
            gallery_path = Path(build_gallery.build(out_path))
            gallery = gallery_path.name
        except Exception as e:
            print(f"Gallery step failed ({e}). Run: "
                  f"python3 build_gallery.py {out_path}")
    elapsed = time.time() - started
    if run_dir:
        manifest = {
            "query": query,
            "started": started_iso,
            "finished": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "duration_seconds": round(elapsed, 1),
            "duration_human": fmt_dur(elapsed),
            "settings": {
                "exact": exact, "scrolls": scrolls, "pace": pace,
                "min_price": min_price, "max_price": max_price,
                "exclude": list(exclude), "keep_all": keep_all,
                "match": match, "limit": limit,
                "stages": stages,
            },
            "search_radius_km": radius_km,
            "search_radius_note": describe_radius(radius_km),
            "locations": locs,
            "per_city": city_stats,
            "unknown_cities": unknown_cities,
            "scrolling": {
                "ceiling_per_city": scrolls,
                "keeper_patience": KEEPER_PATIENCE,
                "scrolls_used": scroll_used,
                "scrolls_possible": scroll_possible,
                "cards_examined": cards_seen,
                "seconds_saved_estimate": round(scroll_saved, 1),
            },
            "unique_listings": len(rows),
            "dropped": dropped_total,
            "drop_reasons": drop_reasons,
            "duplicates_collapsed": dup_collapsed,
            "descriptions_captured": described,
            "interrupted_during_descriptions": interrupted,
            "images_local": sum(1 for r in rows
                                if (r.get("image") or "").startswith(f"{thumbs_path.name}/")),
            "files": {"csv": out_path.name, "gallery": gallery,
                      "thumbnails": thumbs_path.name,
                      "database": str(DB_PATH)},
        }
        if prev_by_id:
            manifest["saved_search"] = {
                "new_listings": len(new_ids),
                "carried_from_previous_runs": len(rows) - len(feed_ids),
                "listings_verified": verified_count,
                "removed": [{"item_id": r.get("item_id"), "title": r.get("title"),
                             "price": r.get("price"), "url": r.get("url"),
                             "removal": r.get("removal"), "marker": r.get("marker")}
                            for r in removed],
            }
        (run_dir / "run.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(f"Run folder: {run_dir}")
    print(f"\nFinished in {fmt_dur(elapsed)}.")
    if gallery_path and open_gallery:
        # The gallery is self-contained (images baked in), so the file URL is
        # all the browser needs.
        print(f"Opening {gallery_path}")
        try:
            webbrowser.open(gallery_path.resolve().as_uri())
        except Exception as e:
            print(f"  couldn't open a browser ({e}); open the file yourself.")
    return {
        "status": "ok",
        "query": query,
        "run_dir": str(run_dir) if run_dir else None,
        "csv": str(out_path),
        "gallery": str(gallery_path) if gallery_path else None,
        "started": started_iso,
        "finished": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_seconds": round(elapsed, 1),
        "new_ids": new_ids,
        "total_ids": [r["item_id"] for r in rows],
        "new_rows": [r for r in rows if r["item_id"] in set(new_ids)],
        "removed": removed,
        "listings_verified": verified_count,
        "described_ids": described_ids,
        "descriptions_fetched": described,
        "interrupted": interrupted,
        "radius_km": radius_km,
        "per_city": city_stats,
        "locations": locs,
        "unknown_cities": unknown_cities,
    }


# ---------- description retrieval (detail pages) ----------
def description_seconds_each(pace, with_thumbs=True):
    lo, hi = PACES[pace]
    return (PAGE_WORK_SECONDS + (PHOTO_SAVE_SECONDS if with_thumbs else 0)
            + (lo + hi) / 2)


def confirm_description_count(targets, pace, budget_minutes, assume_yes=False,
                        with_thumbs=True):
    """Retrieving descriptions is the expensive stage — a few thousand listings
    is hours of detail-page visits. Anything over the budget asks first instead
    of silently committing the evening to it."""
    per = description_seconds_each(pace, with_thumbs)
    est = len(targets) * per / 60
    if assume_yes or not budget_minutes or est <= budget_minutes:
        return targets
    fits = max(1, int(budget_minutes * 60 / per))
    print(f"\n!! Retrieving descriptions for all {len(targets)} listings would "
          f"take about {fmt_dur(est * 60)} at '{pace}' pacing.")
    print(f"   The top {fits} by relevance fit inside your "
          f"{budget_minutes}-minute budget.")
    try:
        ans = input(f"   [Enter] top {fits}  /  (a)ll  /  (s)kip descriptions: ").strip().lower()
    except EOFError:
        ans = ""
    if ans.startswith("a"):
        return targets
    if ans.startswith("s"):
        print("   Skipping this stage. Descriptions and local photos will be "
              "missing.")
        return []
    print(f"   Retrieving descriptions for the top {fits}.")
    return targets[:fits]


def detail_from_json(page):
    """Pull description/photo from the JSON blobs on a listing detail page.
    Far more reliable than og: meta tags, which Facebook often omits for
    logged-in sessions."""
    desc, img = "", ""
    for t in page.eval_on_selector_all('script[type="application/json"]', SCRIPT_JSON_JS):
        if "redacted_description" not in t and "listing_photos" not in t:
            continue
        for doc in iter_json_docs(t):
            if not desc:
                for rd in find_key(doc, "redacted_description"):
                    if isinstance(rd, dict) and rd.get("text"):
                        desc = rd["text"]
                        break
            if not img:
                for photos in find_key(doc, "listing_photos"):
                    if isinstance(photos, list):
                        for ph in photos:
                            uri = ((ph or {}).get("image") or {}).get("uri")
                            if uri:
                                img = uri
                                break
                    if img:
                        break
        if desc and img:
            break
    return desc, img


def wait_for_detail(page, timeout_s=8.0):
    """Poll for the listing payload instead of sleeping a fixed interval — the
    JSON is in the initial HTML, so it's usually ready in well under a second.
    Photos present with no description text means the listing simply has none."""
    start = time.time()
    desc = img = ""
    while time.time() - start < timeout_s:
        desc, img = detail_from_json(page)
        if desc:
            break
        if img and time.time() - start > 2.0:
            break
        time.sleep(0.4)
    return desc, img


def retrieve_descriptions(ctx, page, targets, thumbs_path=None, debug_dump=False,
                pace=DEFAULT_PACE, on_row=None):
    """Visit each target's detail page once: description, full-size photo, and
    (when thumbs_path is given) the photo saved to disk immediately, while its
    URL is only seconds old.

    `on_row` is called with each listing as soon as it is done, so the caller
    can persist it. This stage is the long pole of a run and gets interrupted
    often; nothing gathered should depend on reaching the end.

    Returns False if Ctrl-C ended it early, True otherwise."""
    lo, hi = PACES[pace]
    per = description_seconds_each(pace, thumbs_path is not None)
    print(f"\nRetrieving descriptions for {len(targets)} listings — one detail "
          f"page each at '{pace}' pacing ({lo:g}-{hi:g}s between hits), roughly "
          f"{max(1, round(len(targets) * per / 60))} min. "
          "(--limit / --match narrow it; --pace changes the throttle.)")
    if debug_dump:
        DEBUG_DIR.mkdir(exist_ok=True)
    # Park the page somewhere inert before touching routing. We arrive here
    # sitting on the last city's search feed, which has been scrolled dozens of
    # times and is still streaming images and GraphQL for thousands of cards.
    # Turning on interception makes every one of those in-flight requests
    # round-trip to Python, and on Windows — where the driver connection is
    # slower — that backlog is enough to wedge the route call itself before the
    # first listing is ever fetched. about:blank drops the whole feed first.
    try:
        page.goto("about:blank", wait_until="domcontentloaded")
    except Exception:
        pass

    # Only the JSON payload matters here, so drop the photo/video/font requests
    # each detail page would otherwise pull. Routed on the page, not the
    # context, so the thumbnail fetches below still go through.
    def block_heavy_requests(route):
        # Every request on the page round-trips through here, and a request
        # still in flight when the page navigates away can no longer be
        # answered. Letting that raise would surface as a stalled page rather
        # than a dropped request, so failures here are swallowed by design.
        try:
            if route.request.resource_type in ("image", "media", "font"):
                route.abort()
            else:
                route.continue_()
        except Exception:
            pass

    page.route("**/*", block_heavy_requests)
    got, saved, spent = 0, 0, []
    interrupted = False
    try:
        for i, r in enumerate(targets, 1):
            url = r.get("url") or f"https://www.facebook.com/marketplace/item/{r.get('item_id', '')}"
            print(f"  [{i}/{len(targets)}] {url}")
            t0 = time.time()
            if not goto_with_retry(page, url):
                human_pause(lo, hi)
                continue
            try:
                desc, img = wait_for_detail(page)

                def meta(prop):
                    el = page.query_selector(f'meta[property="{prop}"]')
                    return (el.get_attribute("content") or "") if el else ""
                img = img or meta("og:image")
                desc = desc or meta("og:description")
                if debug_dump and not desc:
                    dump = DEBUG_DIR / f"detail_{r.get('item_id', i)}"
                    dump.mkdir(parents=True, exist_ok=True)
                    scripts = page.eval_on_selector_all(
                        'script[type="application/json"]', SCRIPT_JSON_JS)
                    (dump / "scripts.json").write_text(json.dumps(scripts), encoding="utf-8")
                if img:
                    r["image"] = img
                if desc:
                    r["description"] = desc[:2000]
                    got += 1
                stored = False
                if thumbs_path and (r.get("image") or "").startswith("http"):
                    rel = save_image(ctx, r["image"], r["item_id"], thumbs_path)
                    if rel:
                        r["image"] = rel
                        saved += 1
                        stored = True
                note = f"{len(desc)} chars" if desc else "no description found"
                if stored:
                    note += ", photo saved"
                print(f"     {note} ({time.time() - t0:.1f}s)")
            except Exception as e:
                print(f"     failed: {e}")
            if on_row:
                on_row(r)
            spent.append(time.time() - t0)
            human_pause(lo, hi)
    except KeyboardInterrupt:
        interrupted = True
        print(f"\n  Interrupted after {len(spent)} of {len(targets)} listings. "
              "Everything gathered so far is saved; finishing up the outputs.")
    finally:
        # A Windows console sends Ctrl-C to every process sharing it, which
        # includes Playwright's driver, so by the time we get here the browser
        # may already be gone and this raises. That exception would replace the
        # clean return above and take the whole run with it — losing the CSV and
        # gallery for work that was already done and saved.
        try:
            page.unroute("**/*")
        except Exception:
            pass
    avg = sum(spent) / len(spent) if spent else 0
    print(f"  {got}/{len(spent) if interrupted else len(targets)} descriptions"
          + (f", {saved} photos saved" if thumbs_path else "")
          + f" ({avg:.1f}s avg per page, plus the {lo:g}-{hi:g}s pause between hits).")
    return not interrupted


# ---------- thumbnails (beat URL expiry) ----------
def save_image(ctx, url, item_id, outdir):
    """Download one image with the logged-in session's cookies. Returns the
    path to store in the CSV, or "" on failure."""
    try:
        resp = ctx.request.get(url, timeout=20000)
        if not resp.ok:
            return ""
        ct = (resp.headers.get("content-type", "") or "").lower()
        if "image" not in ct:  # an error page saved as .jpg is worse than nothing
            return ""
        ext = ".png" if "png" in ct else ".webp" if "webp" in ct else ".jpg"
        outdir.mkdir(parents=True, exist_ok=True)
        fp = outdir / f"{item_id}{ext}"
        fp.write_bytes(resp.body())
        return f"{outdir.name}/{fp.name}"
    except Exception:
        return ""


def fetch_thumbs(ctx, rows, outdir):
    """Download any remaining remote images, reusing whatever is on disk."""
    outdir.mkdir(parents=True, exist_ok=True)
    existing = {p.stem: p.name for p in outdir.iterdir()
                if p.suffix in (".jpg", ".png", ".webp")}
    todo, reused, local = [], 0, 0
    for r in rows:
        img = r.get("image") or ""
        if img.startswith(f"{outdir.name}/"):
            local += 1
        elif r.get("item_id") in existing:
            r["image"] = f"{outdir.name}/{existing[r['item_id']]}"
            reused += 1
        elif img.startswith("http"):
            todo.append(r)
    print(f"\nThumbnails: {local} saved while retrieving descriptions, "
          f"{reused} already on disk, {len(todo)} to fetch.")
    ok = 0
    for i, r in enumerate(todo, 1):
        rel = save_image(ctx, r["image"], r["item_id"], outdir)
        if rel:
            r["image"] = rel
            ok += 1
        if i % 25 == 0:
            print(f"   {i}/{len(todo)}...")
        human_pause(0.3, 0.8)
    if todo:
        print(f"  downloaded {ok}/{len(todo)} "
              "(rows whose fetch failed keep their remote URL).")


# ---------- standalone stages (one-off use on an existing CSV) ----------
def read_unique(src):
    with open(src, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    seen, uniq = set(), []
    for r in rows:
        iid = r.get("item_id")
        if iid and iid not in seen:
            seen.add(iid)
            uniq.append(r)
    return rows, uniq


def descriptions_from_csv(csv_in, match, limit=None, debug_dump=False, thumbs_dir=None,
           pace=DEFAULT_PACE):
    started = time.time()
    src = Path(csv_in)
    rows, uniq = read_unique(src)
    targets = [r for r in uniq if (not match or match.lower() in (r.get("title", "").lower()))]
    if limit:
        targets = targets[:limit]
    out_fields = list(dict.fromkeys(list(rows[0].keys()) if rows else FIELDS))
    for extra in ("image", "description"):
        if extra not in out_fields:
            out_fields.append(extra)
    thumbs_path = None
    if thumbs_dir:
        thumbs_path = Path(thumbs_dir)
        if not thumbs_path.is_absolute():
            thumbs_path = src.resolve().parent / thumbs_path
    with keep_awake(), sync_playwright() as p:
        ctx = launch_context(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        ensure_logged_in(page)
        retrieve_descriptions(ctx, page, targets, thumbs_path, debug_dump, pace)
        ctx.close()
    out = src.with_name(src.stem + "_with_descriptions.csv")
    write_csv(uniq, out, out_fields)
    print(f"Wrote {out}. Finished in {fmt_dur(time.time() - started)}.")


def download_thumbs(csv_in, outdir):
    started = time.time()
    src = Path(csv_in)
    rows, uniq = read_unique(src)
    od = Path(outdir)
    if not od.is_absolute():
        od = src.resolve().parent / od
    with keep_awake(), sync_playwright() as p:
        ctx = launch_context(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        ensure_logged_in(page)  # gives the request context your session cookies
        fetch_thumbs(ctx, uniq, od)
        ctx.close()
    out = src.with_name(src.stem + "_local.csv")
    write_csv(uniq, out, list(uniq[0].keys()) if uniq else FIELDS)
    print(f"Wrote {out}. Finished in {fmt_dur(time.time() - started)}.")


def login_only():
    """Refresh the saved Facebook session and nothing else.

    The session lives in this app's own browser profile, so logging in with Safari
    or Chrome does nothing for it. Without this, the only way to renew it was to
    start a sweep and then abandon it — which risks killing the browser before it
    has written the new session to disk."""
    print("Opening Facebook. If you're already logged in, this finishes by itself.")
    with sync_playwright() as p:
        ctx = launch_context(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        ensure_logged_in(page)
        # Closing the context is what writes the session to .fb_session.
        ctx.close()
    print("\nLogged in, and the session is saved. Searches — including scheduled "
          "ones — will use it until Facebook expires it, usually a few weeks.")


def set_radius():
    """The Marketplace search radius is an account setting, not a URL parameter,
    so this opens the UI and waits for you to change it — then confirms the new
    value from the page's own filter payload. Normally you want this pinned at
    the 500-mile maximum, which is what the saved city spacing is built around."""
    seg = next(iter(load_locations().values()))
    with sync_playwright() as p:
        ctx = launch_context(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        ensure_logged_in(page)
        page.goto(build_search_url(seg, "test", False), wait_until="domcontentloaded")
        human_pause(3.0, 5.0)
        before = read_radius_km(page)
        print(f"\nCurrent radius: {describe_radius(before) or 'unknown'}")
        if before == EXPECTED_RADIUS_KM:
            print("That's the 500-mile maximum, which is what the saved city "
                  "spacing assumes — no change needed unless you want it smaller.")
        print(">> In the browser window, open the location/radius control in the "
              "left sidebar and set the radius. This is an account setting, so "
              "it sticks for every future run. Waiting up to 5 minutes...")
        deadline = time.time() + 300
        while time.time() < deadline:
            time.sleep(5)
            now = read_radius_km(page)
            if now and now != before:
                print(f">> Radius is now {describe_radius(now)}. Done.")
                break
        else:
            print(">> Radius unchanged. You can re-run --set-radius any time.")
        ctx.close()


def run_from_ui(a):
    """Collect settings from the pre-flight window, then run with them.
    Command-line values seed the form, so --query x --ui opens it pre-filled."""
    import settings_ui
    import scheduling
    locs = load_locations()

    def ui_add_city(label, text):
        updated, err = add_location(label, text)
        return (list(updated.keys()) if updated else list(locs)), err

    def ui_remove_city(label):
        updated, err = remove_location(label)
        return list(updated.keys()), err

    cfg = settings_ui.collect_settings(
        list(locs.keys()), PACES,
        {"query": a.query or "", "exclude": a.exclude or "", "pace": a.pace,
         "page_work": PAGE_WORK_SECONDS, "photo_save": PHOTO_SAVE_SECONDS,
         "descriptions_budget": a.descriptions_budget},
        on_add=ui_add_city, on_remove=ui_remove_city,
        builtins=list(base_locations()),
        hooks=scheduling.ui_hooks())
    if not cfg:
        print("Cancelled — nothing was run.")
        return
    # "Run now" on a saved search has to wait for this window to close, because
    # the window is holding the one Chromium profile the session lives in.
    if cfg.get("action") == "run_saved":
        scheduling.tick(force=cfg["id"])
        return
    print(f"\nStarting: query '{cfg['query']}', {len(cfg['cities'])} "
          f"cit{'y' if len(cfg['cities']) == 1 else 'ies'}.")
    try:
        with scheduling.run_lock("a manual run"):
            run(cfg["query"], DEFAULT_SCROLLS, cfg["exact"], a.out, None,
                a.keep_all, cfg["debug_dump"], a.match, cfg["limit"],
                a.thumbs_dir,
                do_descriptions=cfg["do_descriptions"], do_thumbs=cfg["do_thumbs"],
                do_gallery=cfg["do_gallery"], pace=cfg["pace"],
                exclude=[t.strip() for t in cfg["exclude"].split(",") if t.strip()],
                min_price=cfg["min_price"], max_price=cfg["max_price"],
                descriptions_budget=cfg["descriptions_budget"], assume_yes=a.yes,
                only_labels=cfg["cities"], open_gallery=not a.no_open,
                no_pause=a.no_pause)
    except scheduling.AlreadyRunning as e:
        print(f"\nNot starting: {e}.\nBoth runs would need the same Facebook "
              f"session, so wait for that one to finish.")


def main():
    # The terminal is the only progress indicator during a run that lasts
    # hours, so it must never sit in a buffer. Python line-buffers a console
    # already; this covers the cases where it doesn't and silence would read as
    # a hang.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    ap = argparse.ArgumentParser(
        description="Personal-use FB Marketplace sweep. A plain --query run does "
                    "everything: sweep, descriptions, thumbnails, gallery.")
    ap.add_argument("--query", help="search query (required for a run)")
    ap.add_argument("--import-urls", metavar="FILE")
    ap.add_argument("--out", metavar="CSV",
                    help="explicit CSV path; skips the per-run runs/<query>_<date>/ folder")
    ap.add_argument("--exclude", metavar="TERMS", default="",
                    help="comma-separated terms to reject, matched ignoring spaces "
                         "and punctuation so 'can am' also kills 'Can-Am' and 'CANAM'")
    ap.add_argument("--min-price", type=int, metavar="N",
                    help="drop listings under N dollars (also sent to Facebook)")
    ap.add_argument("--max-price", type=int, metavar="N",
                    help="drop listings over N dollars (also sent to Facebook)")
    # The old --enrich* spellings stay on as hidden aliases so existing notes
    # and scripts keep working.
    ap.add_argument("--descriptions-budget", "--enrich-budget", type=int,
                    metavar="MIN", dest="descriptions_budget",
                    default=DEFAULT_DESCRIPTIONS_BUDGET_MIN,
                    help="ask before retrieving descriptions would take longer "
                         "than MIN minutes (default 0, which never asks)")
    ap.add_argument("--yes", action="store_true",
                    help="don't ask about long description jobs, just run them")
    ap.add_argument("--no-pause", action="store_true",
                    help="skip the pause after login for popups and the radius")
    ap.add_argument("--no-open", action="store_true",
                    help="don't open the finished gallery in a browser")
    ap.add_argument("--set-radius", action="store_true",
                    help="open Marketplace so you can check/change the account search radius")
    ap.add_argument("--login", action="store_true",
                    help="log into Facebook and save the session, without running a search")
    ap.add_argument("--ui", action="store_true",
                    help="open the settings window (the default when run with no arguments)")
    ap.add_argument("--no-ui", action="store_true",
                    help="never open the settings window, even with no arguments")
    ap.add_argument("--only", metavar="LABEL", help="run only locations whose label contains LABEL")
    ap.add_argument("--match", metavar="TERM", help="only describe listings whose title contains TERM")
    ap.add_argument("--limit", type=int, metavar="N",
                    help="only retrieve descriptions for the first N listings")
    # --thumbs-dir is the old name, kept so existing command lines don't break.
    ap.add_argument("--thumbnails-dir", "--thumbs-dir", default=THUMBS_DIRNAME,
                    dest="thumbs_dir", metavar="DIR",
                    help=f"thumbnail folder (default: {THUMBS_DIRNAME})")
    ap.add_argument("--pace", choices=list(PACES), default=DEFAULT_PACE,
                    help="pause between detail-page hits while retrieving "
                         "descriptions: "
                         f"fast (1-2.5s, ~7s per listing, default), "
                         f"slow (3-5s, ~9s per listing)")
    ap.add_argument("--scrolls", type=int, default=DEFAULT_SCROLLS,
                    help=f"max scrolls per city (safety ceiling, default {DEFAULT_SCROLLS}; "
                         f"normally stops much sooner, after {KEEPER_PATIENCE} scrolls "
                         "with no new matches)")
    ap.add_argument("--exact", action="store_true", help="tight matching (default loose)")
    ap.add_argument("--keep-all", action="store_true",
                    help="keep outside-search and non-matching listings instead of dropping them")
    ap.add_argument("--no-descriptions", "--no-enrich", action="store_true",
                    dest="no_descriptions", help="skip the detail-page stage")
    ap.add_argument("--no-thumbs", action="store_true", help="skip downloading images")
    ap.add_argument("--no-gallery", action="store_true", help="skip building gallery.html")
    ap.add_argument("--debug-dump", action="store_true",
                    help="save raw Facebook JSON payloads to debug/ for troubleshooting")
    ap.add_argument("--descriptions", "--enrich", metavar="CSV",
                    dest="descriptions",
                    help="one-off: retrieve descriptions for an existing CSV "
                         "instead of running a sweep")
    ap.add_argument("--download-thumbs", metavar="CSV",
                    help="one-off: download the image URLs in an existing CSV")
    a = ap.parse_args()
    exclude = [t.strip() for t in a.exclude.split(",") if t.strip()]
    if a.import_urls:
        import_urls(a.import_urls)
    elif a.login:
        login_only()
    elif a.set_radius:
        set_radius()
    elif a.descriptions:
        descriptions_from_csv(a.descriptions, a.match, a.limit, a.debug_dump,
                              None if a.no_thumbs else a.thumbs_dir, a.pace)
    elif a.download_thumbs:
        download_thumbs(a.download_thumbs, a.thumbs_dir)
    elif a.ui or (not a.query and not a.no_ui):
        run_from_ui(a)
    else:
        if not a.query:
            ap.error("--query is required (no silent default)")
        run(a.query, a.scrolls, a.exact, a.out, a.only, a.keep_all, a.debug_dump,
            a.match, a.limit, a.thumbs_dir,
            do_descriptions=not a.no_descriptions, do_thumbs=not a.no_thumbs,
            do_gallery=not a.no_gallery, pace=a.pace, exclude=exclude,
            min_price=a.min_price, max_price=a.max_price,
            descriptions_budget=a.descriptions_budget, assume_yes=a.yes,
            open_gallery=not a.no_open, no_pause=a.no_pause)


if __name__ == "__main__":
    main()
