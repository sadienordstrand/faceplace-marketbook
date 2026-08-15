#!/usr/bin/env python3
"""
scheduling.py
-------------
Searches that run themselves on a schedule and email you the results.

    python3 src/scheduling.py --tick          # run whatever is due (what the OS calls)
    python3 src/scheduling.py --list          # show scheduled searches and when they run
    python3 src/scheduling.py --run NAME      # run one now, ignoring its schedule
    python3 src/scheduling.py --install       # let the OS wake the machine and run ticks
    python3 src/scheduling.py --uninstall
    python3 src/scheduling.py --test-email
    python3 src/scheduling.py --verify-probe URL
    python3 src/scheduling.py --serve-galleries  # localhost server for email links

Everything lives in one module on purpose: launchd and Task Scheduler need a
single entry point to call, and the runner, the schedule arithmetic and the
report all have to agree about the same state files.

Times are naive local wall-clock throughout. A daily search means 5am the way a
person means it, on both sides of a daylight-saving change, and every part of
this runs on one laptop, so wall clock is the honest unit. UTC would make 5am
drift by an hour twice a year.
"""
import argparse
import base64
import json
import os
import platform
import re
import smtplib
import socket
import ssl
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import browser
import descriptions
import listings
import paths
import storage

# The project folder someone opens, which is not where this file lives — see the
# note in paths.py. Anything user-facing about "this folder" means this one.
ROOT = paths.ROOT
# Rebound as module names because that's what the tests redirect.
SEARCHES_PATH = paths.SEARCHES_PATH
EMAIL_CONFIG_PATH = paths.EMAIL_CONFIG_PATH
SCHEDULE_DIR = paths.SCHEDULE_DIR
LOCK_PATH = SCHEDULE_DIR / "run.lock"
TICK_LOG = SCHEDULE_DIR / "tick.log"
RUNS_DIR = paths.RUNS_DIR

# Gmail (and most other webmail) strips file:// hrefs before the message is
# shown, which is why a perfectly valid <a> arrives as ordinary text. An
# http://127.0.0.1 link survives that, and only does anything on this computer
# — same as the file it points at. The server that answers it is started by
# every tick and by --install.
GALLERY_HOST = "127.0.0.1"
GALLERY_PORT = 18741
GALLERY_NAMES = ("gallery.html", "lightweight_gallery.html")


def _support_dir():
    """A folder the OS lets a background task write to wherever this project
    happens to live.

    macOS denies a launchd agent every file under Documents, Desktop and
    Downloads. If the scheduler's own log lived beside the code, the one message
    explaining that refusal would itself be refused, which is exactly the silence
    that made a broken install look like a working one."""
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "FaceplaceMarketbook"
    if system == "Windows":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "FaceplaceMarketbook"
    return Path.home() / ".local" / "share" / "FaceplaceMarketbook"


SUPPORT_DIR = _support_dir()
AGENT_LOG = SUPPORT_DIR / "scheduler.log"
HEARTBEAT_PATH = SUPPORT_DIR / "last-checkin.json"

# Folders macOS puts behind a permission prompt no background task can answer.
MAC_PROTECTED = ("Documents", "Desktop", "Downloads")

# How often the OS wakes us up to look for due searches. Anything finer just
# spins the disk; anything coarser makes "every 6 hours" mean "every 6 hours,
# give or take an hour".
TICK_SECONDS = 900

# Daily searches fire at this hour, local.
DAILY_HOUR = 5

# Hour-interval searches run at fixed times of day: DAILY_HOUR and then every so
# many hours after it, the same times every day. The times have to be knowable
# in advance because waking a sleeping Mac takes a queue of scheduled wake-ups
# written days ahead (see the wake queue below), and a schedule computed from
# whenever the last run happened to start would drift away from any queue within
# a day. Divisors of 24, so the grid really is the same every day.
HOUR_CHOICES = (3, 4, 6, 8, 12)

# The wake queue: how many days of wake-ups get written at once, when the window
# starts offering to renew them, and when report emails start saying so. The
# horizon is deliberately short — these events are the only thing the app leaves
# on a machine that can outlive the folder being dragged to the trash, so three
# weeks of ghost wake-ups is the worst case rather than a permanent modification.
WAKE_HORIZON_DAYS = 21
WAKE_RENEW_BELOW_DAYS = 7
WAKE_NAG_BELOW_DAYS = 3
WAKE_OWNER = "Faceplace Marketbook"
# A safety valve, not a target: the union of several searches' grids can get
# dense, and the system's scheduled-events list is shared with every other
# program on the machine.
WAKE_MAX_EVENTS = 200

# A run this far past its scheduled time gets reported as late, which is how a
# laptop that was asleep or switched off shows up in your inbox.
LATE_AFTER_HOURS = 2

# Advisory thresholds. Frequent automated sweeps are what gets Marketplace
# accounts limited, so the UI warns past these but never refuses.
SAFE_MIN_INTERVAL_HOURS = 6
SAFE_MAX_SEARCHES = 3
SAFE_MAX_QUERIES = 2

# A lock older than this is assumed to belong to a process that died without
# cleaning up. Longer than any real run: a wide multi-city sweep with
# descriptions is a couple of hours.
LOCK_STALE_HOURS = 8

# "minutes" exists for the test suite, which cannot wait an hour to prove that
# the second run of a search reuses its folder. The settings window only offers
# it when FACEPLACE_DEV=1, so nobody can pick it by accident.
UNITS = ("minutes", "hours", "days")
DEV_MODE = os.environ.get("FACEPLACE_DEV") == "1"

SMTP_HOSTS = {
    "gmail": ("smtp.gmail.com", 587),
    "outlook": ("smtp-mail.outlook.com", 587),
    "icloud": ("smtp.mail.me.com", 587),
}


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        SCHEDULE_DIR.mkdir(parents=True, exist_ok=True)
        with open(TICK_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def check_in(event, **extra):
    """Leave a mark outside the project folder saying the OS actually reached us.

    Without it, an agent that macOS started and then denied every file looks
    identical to an agent that ran and found nothing due. The install checks for
    this, and so does the settings window."""
    payload = {"event": event, "at": f"{datetime.now():%Y-%m-%dT%H:%M:%S}",
               "pid": os.getpid(), "python": sys.executable, "folder": str(ROOT),
               **extra}
    try:
        SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass
    return payload


def last_check_in():
    try:
        return json.loads(HEARTBEAT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---------- time helpers ----------
def now_local():
    return datetime.now().replace(microsecond=0)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S") if dt else None


def parse_iso(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is not None:
        # Convert, don't just drop the offset. The sweep records its start time in
        # UTC, and reading that as local wall clock put a run that happened at
        # 11:12pm into a report as "started tomorrow at 5:12am".
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.replace(microsecond=0)


def _hour12(dt):
    # %-I is not portable to Windows, so format the hour by hand.
    h = dt.hour % 12 or 12
    return f"{h}:{dt:%M}{'am' if dt.hour < 12 else 'pm'}"


def fmt_when(dt):
    if not dt:
        return "never"
    today = now_local().date()
    if dt.date() == today:
        return f"today at {_hour12(dt)}"
    if dt.date() == today + timedelta(days=1):
        return f"tomorrow at {_hour12(dt)}"
    if dt.date() == today - timedelta(days=1):
        return f"yesterday at {_hour12(dt)}"
    return f"{dt:%a %b} {dt.day} at {_hour12(dt)}"


def fmt_dur(seconds):
    seconds = int(round(seconds or 0))
    h, m, s = seconds // 3600, (seconds % 3600) // 60, seconds % 60
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def interval_hours(interval):
    n = max(1, int(interval.get("every") or 1))
    unit = interval.get("unit") or "days"
    return n * {"minutes": 1 / 60, "hours": 1.0, "days": 24.0}.get(unit, 24.0)


def describe_interval(interval):
    n = max(1, int(interval.get("every") or 1))
    unit = (interval.get("unit") or "days").rstrip("s")
    return f"every {unit}" if n == 1 else f"every {n} {unit}s"


# ---------- scheduled searches ----------
DEFAULT_SEARCH = {
    "enabled": True,
    # queries is the search; query is the same thing on one line, kept because
    # the reports, the logs and the CSV all want a name for the whole search.
    "queries": [],
    "query": "",
    "cities": [],
    "exact": False,
    "min_price": None,
    "max_price": None,
    "min_year": None,
    "max_year": None,
    "include_no_year": True,
    "radius_miles": None,
    "exclude": "",
    "do_descriptions": True,
    "do_thumbs": True,
    "pace": "fast",
    "limit": None,
    "interval": {"every": 1, "unit": "days"},
    "email_to": "",
    "last_started": None,
    "last_finished": None,
    "next_run": None,
}


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_") or "search"


# What a search without a radius is taken to mean. The settings window requires
# one and offers no default, so this is only ever reached by a search saved
# before the field existed or edited by hand — and 500, Facebook's maximum, is
# what the shipped city spacing already assumes. It errs toward searching wider
# than intended rather than quietly narrower, which is the failure that has no
# symptom: a run that misses listings looks exactly like one that found none.
DEFAULT_RADIUS_MILES = 500


def normalize_search(search):
    """Fill in the defaults, and settle what this search is looking for.

    `queries` is the authority and `query` is derived from it, so the two can
    never drift apart. A search saved before queries existed, or one edited by
    hand with only a query in it, gets its list from that single line. A search
    from before radius existed is filled the same way — absent and null alike,
    since a hand-edited file can hold either."""
    rec = {**DEFAULT_SEARCH, **search}
    rec["queries"] = listings.query_list(
        rec.get("queries") or rec.get("query"))[:listings.MAX_QUERIES]
    rec["query"] = listings.query_label(rec["queries"])
    if rec.get("radius_miles") is None:
        rec["radius_miles"] = DEFAULT_RADIUS_MILES
    return rec


def load_searches():
    if not SEARCHES_PATH.exists():
        return []
    try:
        data = json.loads(SEARCHES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        # Never silently start from scratch: that would quietly delete every
        # scheduled search the moment the file got a stray character in it.
        raise SystemExit(f"{SEARCHES_PATH.name} is unreadable ({e}). Fix or move "
                         f"the file; it has not been touched.")
    searches = data.get("searches", []) if isinstance(data, dict) else data
    return [normalize_search(s) for s in searches]


def save_searches(searches):
    payload = {"version": 1, "searches": searches}
    tmp = SEARCHES_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(SEARCHES_PATH)
    return searches


def find_search(searches, ref):
    """Look a search up by id, then by exact name, then case-insensitively, so
    --run works with whatever the user actually typed."""
    for s in searches:
        if s.get("id") == ref:
            return s
    for s in searches:
        if s.get("name") == ref:
            return s
    low = (ref or "").strip().lower()
    for s in searches:
        if (s.get("name") or "").lower() == low:
            return s
    return None


def validate_bounds(search):
    """The price and year ranges, checked the same way the settings window
    checks them. The window can't be the only guard: a scheduled search also arrives
    from a hand-edited saved_searches.json, and a range that can't match
    anything runs for just as long as a good one before returning nothing."""
    lo_p, hi_p = search.get("min_price"), search.get("max_price")
    for label, val in (("Minimum", lo_p), ("Maximum", hi_p)):
        if val is not None and val < 0:
            return f"{label} price can't be negative."
    if lo_p is not None and hi_p is not None and lo_p > hi_p:
        return "The minimum price is higher than the maximum price."
    lo_y, hi_y = search.get("min_year"), search.get("max_year")
    latest = listings.latest_year()
    for val in (lo_y, hi_y):
        if val is not None and not listings.EARLIEST_YEAR <= val <= latest:
            return f"Years have to be between {listings.EARLIEST_YEAR} and {latest}."
    if lo_y is not None and hi_y is not None and lo_y > hi_y:
        return "The minimum year is later than the maximum year."
    # Facebook's picker offers a fixed list, and asking for anything else means
    # the run would silently keep whatever radius was already set. Imported here
    # rather than at the top for the usual reason in this file: the sweep
    # imports us, so we can only reach back into it once something calls in.
    import fb_marketplace_sweep as fb

    r = search.get("radius_miles")
    if r is not None and r not in fb.RADIUS_CHOICES_MILES:
        return ("The search radius has to be one of "
                + ", ".join(str(m) for m in fb.RADIUS_CHOICES_MILES) + " miles.")
    return None


def validate_search(search, searches, editing_id=None):
    name = (search.get("name") or "").strip()
    if not name:
        return "Give the search a name."
    if not listings.query_list(search.get("queries") or search.get("query")):
        return "A scheduled search needs something to search for."
    if not search.get("cities"):
        return "Pick at least one city."
    for s in searches:
        if s.get("id") != editing_id and (s.get("name") or "").lower() == name.lower():
            return f"You already have a scheduled search called '{name}'."
    err = validate_bounds(search)
    if err:
        return err
    iv = search.get("interval") or {}
    if iv.get("unit") not in UNITS:
        return f"Interval unit must be one of: {', '.join(UNITS)}."
    try:
        if int(iv.get("every")) < 1:
            return "Run it at least once per interval — use 1 or more."
    except (TypeError, ValueError):
        return "The interval number must be a whole number."
    to = (search.get("email_to") or "").strip()
    if to and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", to):
        return f"'{to}' doesn't look like an email address."
    return None


def interval_warnings(search, searches):
    """Advisory only. Frequent sweeps and lots of scheduled searches are the two
    things most likely to get an account limited, so say so at the moment
    someone sets one up."""
    out = []
    hours = interval_hours(search.get("interval") or {})
    if hours < SAFE_MIN_INTERVAL_HOURS:
        out.append(
            f"Running more often than every {SAFE_MIN_INTERVAL_HOURS} hours puts a "
            f"lot of automated traffic on your Facebook account, which raises the "
            f"risk of it being limited or banned. Somewhere between "
            f"{SAFE_MIN_INTERVAL_HOURS} and 24 hours is a safer choice.")
    enabled = sum(1 for s in searches
                  if s.get("enabled") and s.get("id") != search.get("id"))
    if enabled + 1 > SAFE_MAX_SEARCHES:
        out.append(
            f"You already have {enabled} other active scheduled searches. Too "
            f"much automated traffic on your Facebook account may raise the "
            f"risk of it being limited or banned, so proceed with caution.")
    n = len(listings.query_list(search.get("queries") or search.get("query")))
    if n > SAFE_MAX_QUERIES:
        out.append(
            f"This search has {n} queries, which means it takes about {n} times "
            f"as long. Too much automated traffic on your Facebook account may "
            f"raise the risk of it being limited or banned, so proceed with "
            f"caution.")
    return out


def add_search(search):
    searches = load_searches()
    # Fill the defaults in before validating, so a caller that only supplies a
    # name, query and cities isn't told its interval is invalid.
    rec = normalize_search(
        {k: v for k, v in search.items() if v is not None})
    err = validate_search(rec, searches)
    if err:
        return None, err
    rec["name"] = rec["name"].strip()
    suffix = base64.b32encode(os.urandom(3)).decode("ascii").lower()[:4]
    rec["id"] = f"{slugify(rec['name'])[:40]}-{suffix}"
    rec["created"] = iso(now_local())
    rec["next_run"] = iso(next_run_at(rec))
    searches.append(rec)
    save_searches(searches)
    return rec, None


def update_search(search_id, changes):
    searches = load_searches()
    rec = find_search(searches, search_id)
    if not rec:
        return None, f"No scheduled search with id '{search_id}'."
    merged = {**rec, **changes}
    # A caller changing only the one-line query means it: without this, the
    # queries list it was derived from would win and the change would vanish.
    if "query" in changes and "queries" not in changes:
        merged["queries"] = listings.query_list(changes["query"])
    merged = normalize_search(merged)
    err = validate_search(merged, searches, editing_id=rec.get("id"))
    if err:
        return None, err
    rec.update(merged)
    rec["name"] = rec["name"].strip()
    if "next_run" not in changes:
        # Changing the interval re-times the next run from the last start, so
        # switching daily to hourly takes effect without waiting out the old gap.
        # An explicit next_run wins, which is how "run it now" is expressed.
        rec["next_run"] = iso(next_run_at(rec))
    save_searches(searches)
    return rec, None


def delete_search(search_id):
    searches = load_searches()
    rec = find_search(searches, search_id)
    if not rec:
        return None, f"No scheduled search with id '{search_id}'."
    save_searches([s for s in searches if s.get("id") != rec.get("id")])
    return rec, None


# ---------- when does it run next ----------
def grid_hours(every):
    """The hours of the day an every-N-hours search runs at: DAILY_HOUR and then
    every N hours after it, identical every day. For the offered choices —
    divisors of 24 — the spacing is exact. A legacy value that doesn't divide 24
    still gets a daily-repeating grid; it just has one short gap where the count
    wraps past midnight back to DAILY_HOUR."""
    every = max(1, min(24, int(every or 1)))
    return sorted((DAILY_HOUR + k * every) % 24
                  for k in range(-(-24 // every)))


def next_grid_time(every, after):
    """The first moment on the grid strictly after `after`."""
    day = after.replace(hour=0, minute=0, second=0, microsecond=0)
    for offset in (0, 1):
        for h in grid_hours(every):
            t = day + timedelta(days=offset, hours=h)
            if t > after:
                return t


def next_run_at(search, after=None):
    """Scheduled from when the last run STARTED, not when it finished, so a slow
    run doesn't push every following run later and later."""
    iv = search.get("interval") or {}
    n = max(1, int(iv.get("every") or 1))
    unit = iv.get("unit") or "days"
    now = after or now_local()
    last = parse_iso(search.get("last_started"))

    if unit == "days":
        if last is None:
            # The first run is the next 5am to come around.
            target = now.replace(hour=DAILY_HOUR, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            return target
        target = (last + timedelta(days=n)).replace(
            hour=DAILY_HOUR, minute=0, second=0, microsecond=0)
        # A run that started late, or a machine that woke up late, must not leave
        # the next target in the past where it would fire again immediately.
        while target <= last:
            target += timedelta(days=n)
        return target

    if unit == "hours":
        # The next grid time, not last + N hours. A run that started late — the
        # 5pm slot reached at 6:20 because the Mac was asleep — measures from
        # when it actually started, so the fires it slept through are skipped
        # and the schedule snaps back to the same times as every other day.
        return next_grid_time(n, last or now)

    # Minutes exist for the test suite only, and keep the old free-running
    # arithmetic: a grid is pointless at a scale no wake-up will ever serve.
    step = timedelta(minutes=n)
    if last is None:
        return now
    target = last + step
    # If a run overran its own interval, skip the fires it slept through instead
    # of queueing several at once.
    while target <= now - step:
        target += step
    return target


def is_due(search, now=None):
    if not search.get("enabled"):
        return False
    now = now or now_local()
    nxt = parse_iso(search.get("next_run")) or next_run_at(search, now)
    return nxt <= now


def lateness_hours(search, now=None):
    now = now or now_local()
    nxt = parse_iso(search.get("next_run"))
    return max(0.0, (now - nxt).total_seconds() / 3600) if nxt else 0.0


def due_searches(searches, now=None):
    """Everything due, oldest-created first. Creation order is the queue order,
    and only one ever runs at a time."""
    now = now or now_local()
    due = [s for s in searches if is_due(s, now)]
    due.sort(key=lambda s: (s.get("created") or "", s.get("name") or ""))
    return due


# ---------- email configuration ----------
# There is no address to send to in here. Every scheduled search carries its
# own, chosen when it was made, and a second one at the account level was two
# places to look for the same answer; a search that never had one falls back to
# the account's own address, which is the only address this file knows about.
DEFAULT_EMAIL_CONFIG = {
    "provider": "gmail",
    "host": "",
    "port": 587,
    "address": "",
    "app_password": "",
}


def load_email_config():
    if not EMAIL_CONFIG_PATH.exists():
        return dict(DEFAULT_EMAIL_CONFIG)
    try:
        data = json.loads(EMAIL_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SystemExit(f"{EMAIL_CONFIG_PATH.name} is unreadable ({e}).")
    return {**DEFAULT_EMAIL_CONFIG, **data}


def save_email_config(cfg):
    merged = {**DEFAULT_EMAIL_CONFIG, **cfg}
    # Temp file + replace so a crash mid-write can't leave the config corrupt.
    # An app password is full access to the mailbox, not just permission to
    # send, so it's locked down before the file lands at its real name.
    tmp = EMAIL_CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(EMAIL_CONFIG_PATH)
    return merged


def email_ready(cfg=None):
    cfg = cfg or load_email_config()
    return bool(cfg.get("address") and cfg.get("app_password"))


# Deliberately loose. The job is to catch a typo while the user is still looking
# at the box, not to adjudicate RFC 5322 — the mail server is the only authority
# on whether an address exists, and it gets consulted by the test send.
EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;.]+(\.[^@\s,;.]+)+$")


def address_problem(value, what):
    """A refusal for something that cannot possibly work, or None."""
    value = (value or "").strip()
    if not value:
        return None
    if " " in value:
        return f"{what} has a space in it: '{value}'."
    if "@" not in value:
        return f"{what} needs an @ in it: '{value}'."
    if not EMAIL_RE.match(value):
        return f"{what} doesn't look like an email address: '{value}'."
    return None


def email_remarks(cfg):
    """Things that are probably wrong but might not be, so they're said rather
    than enforced."""
    out = []
    provider = cfg.get("provider") or "gmail"
    # A Google app password is sixteen lowercase letters, shown in groups of four.
    # Checking the shape catches a normal password that happens to be 16 long.
    pw = (cfg.get("app_password") or "").replace(" ", "").replace("-", "")
    if provider == "gmail" and pw and not re.fullmatch(r"[a-z]{16}", pw):
        out.append("That doesn't look like a Google app password, which is "
                   "sixteen lowercase letters shown in four groups of four. Your "
                   "normal Google password will be rejected.")
    return out


def smtp_target(cfg):
    if cfg.get("host"):
        return cfg["host"], int(cfg.get("port") or 587)
    return SMTP_HOSTS.get(cfg.get("provider") or "gmail", SMTP_HOSTS["gmail"])


def send_email(cfg, to, subject, text_body, html_body=None, attachments=(),
               timeout=120):
    """attachments is a sequence of (filename, bytes, subtype)."""
    if not email_ready(cfg):
        raise RuntimeError("Email isn't set up yet — add your email address and "
                           "app password in the settings window.")
    to = (to or cfg["address"]).strip()
    msg = EmailMessage()
    msg["From"] = cfg["address"]
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="faceplace.local")
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    for name, data, subtype in attachments:
        msg.add_attachment(data, maintype="text", subtype=subtype, filename=name)

    host, port = smtp_target(cfg)
    ctx = ssl.create_default_context()
    if int(port) == 465:
        with smtplib.SMTP_SSL(host, port, timeout=timeout, context=ctx) as s:
            s.login(cfg["address"], cfg["app_password"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=timeout) as s:
            s.ehlo()
            s.starttls(context=ctx)
            s.login(cfg["address"], cfg["app_password"])
            s.send_message(msg)
    return msg["Message-ID"]


# ---------- verifying that listings are still up ----------
# Tier 1 asks for the page without a browser: no rendering, no images, no
# JavaScript. That costs about a second against roughly seven for a full detail
# visit, which is what makes checking old listings affordable. A listing that
# turned up in this run's feed is never checked at all, because appearing in the
# feed already proves it exists.
GONE_MARKERS = (
    "this listing isn't available",
    "this listing is no longer available",
    "isn't available right now",
    "content isn't available",
    "page isn't available",
    "the link you followed may be broken",
    "link may be broken",
)
LIVE_MARKERS = (
    "marketplace_listing_title",
    "redacted_description",
    "listing_photos",
    "marketplace_listing_seller",
)
# Flags on a listing's own record. is_pending means a sale is agreed but not
# finalised, which is "gone" as far as anyone shopping is concerned.
ITEM_FLAGS = ("is_sold", "is_pending", "is_live", "is_hidden")
ITEM_ID_RE = re.compile(r"/item/(\d+)")
JSON_SCRIPT_RE = re.compile(
    r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', re.S)
AUTH_URL_RE = re.compile(r"/login|/checkpoint|two_step_verification|/recover", re.I)

# Facebook answers a bare request context with HTTP 400. It wants a request that
# looks like a browser navigation, so the cheap tier has to say so explicitly.
PROBE_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
}

STATUS_LIVE, STATUS_SOLD, STATUS_GONE = "live", "sold", "gone"
STATUS_UNKNOWN, STATUS_AUTH = "unknown", "auth"
DEFINITE = (STATUS_LIVE, STATUS_SOLD, STATUS_GONE)


def listing_record(body, item_id):
    """The target listing's own data, pulled out of the page's embedded JSON.

    This has to be exact, and a substring search cannot be. A listing page also
    carries the "related items" rail — about twenty other listings, each with its
    own is_sold and is_pending flags — plus Facebook's UI string bundles, which
    contain phrases like "marked as sold" on every page. Searching the whole page
    for '"is_sold":true' therefore answers a question nobody asked: is anything
    on this page sold? Measured against real pages it called a live listing sold
    eight times out of nine.

    So find the node whose id is the listing we asked about, and read that."""
    if not item_id:
        return None
    want, fallback = str(item_id), None
    for m in JSON_SCRIPT_RE.finditer(body or ""):
        try:
            block = json.loads(m.group(1))
        except ValueError:
            continue
        # An explicit stack, not recursion: these payloads nest deeply enough to
        # blow the interpreter's limit.
        stack = [block]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if str(node.get("id")) == want:
                    if "is_sold" in node:
                        return node
                    if fallback is None and any(f in node for f in ITEM_FLAGS):
                        fallback = node
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    return fallback


def classify_listing(status_code, final_url, body, item_id=None):
    """Return (status, marker). Only 'sold' and 'gone' ever remove a listing, so
    anything ambiguous has to come back 'unknown' — a timeout, a rate limit or a
    page we can't read must never be mistaken for a deletion."""
    if AUTH_URL_RE.search(final_url or ""):
        return STATUS_AUTH, "redirected to login"
    if status_code in (404, 410):
        return STATUS_GONE, f"HTTP {status_code}"
    low = (body or "").lower()
    if not low:
        return STATUS_UNKNOWN, f"empty body (HTTP {status_code})"
    if status_code >= 400:
        # Facebook answers a request it doesn't like with 400 and a stub page.
        # That says something about the request, not about the listing.
        return STATUS_UNKNOWN, f"HTTP {status_code}"

    rec = listing_record(body, item_id or _id_from_url(final_url))
    if rec is not None:
        if rec.get("is_sold"):
            return STATUS_SOLD, "this listing's own record says is_sold"
        if rec.get("is_pending"):
            return STATUS_SOLD, "this listing's own record says is_pending"
        if rec.get("is_live") is False:
            return STATUS_GONE, "this listing's own record says is_live false"
        return STATUS_LIVE, "this listing's own record says live"

    # No record for this id: either the listing is genuinely gone, or the page
    # changed shape and we simply failed to read it. Measured against real
    # pages, the two are cleanly separable — a removed listing's page carries no
    # listing data at all (zero occurrences of every LIVE_MARKER), while a live
    # listing's page carries no gone phrase anywhere, string bundles included.
    # Requiring both signals means a future change to Facebook's payload degrades
    # to "unknown" rather than to "delete everything".
    if any(m in low for m in LIVE_MARKERS):
        return STATUS_UNKNOWN, "listing page, but no record for this id"
    for m in GONE_MARKERS:
        if m in low:
            return STATUS_GONE, m
    # Facebook sometimes retires a listing by bouncing it to Marketplace's root
    # rather than serving an error page.
    if final_url and "/marketplace/item/" not in final_url:
        return STATUS_GONE, f"redirected to {final_url[:70]}"
    return STATUS_UNKNOWN, f"no marker (HTTP {status_code}, {len(low)} bytes)"


def _id_from_url(url):
    m = ITEM_ID_RE.search(url or "")
    return m.group(1) if m else None


def probe_listing(ctx, url, timeout=20000):
    """Tier 1, through Playwright's request context, which carries the logged-in
    session's cookies without opening a page. About a second, against roughly
    seven for a full detail-page visit — which is what makes re-checking old
    listings affordable at all.

    PROBE_HEADERS is not optional: without them Facebook answers 400."""
    item_id = _id_from_url(url)
    try:
        resp = ctx.request.get(url, timeout=timeout, max_redirects=5,
                               headers=PROBE_HEADERS)
    except Exception as e:
        return STATUS_UNKNOWN, f"request failed: {type(e).__name__}"
    try:
        body = resp.text()
    except Exception:
        body = ""
    return classify_listing(resp.status, resp.url, body, item_id)


def probe_listing_rendered(page, url, timeout=25000):
    """Tier 2, only for the listings Tier 1 couldn't call either way."""
    item_id = _id_from_url(url)
    try:
        resp = page.goto(url, timeout=timeout, wait_until="domcontentloaded")
    except Exception as e:
        return STATUS_UNKNOWN, f"navigation failed: {type(e).__name__}"
    try:
        page.wait_for_timeout(1200)
        body = page.content()
    except Exception:
        body = ""
    return classify_listing(resp.status if resp else 0, page.url, body, item_id)


def make_verifier(con, pause, page=None, on_progress=None):
    """Returns the function the sweep calls with the listings that went missing
    from the feed. Passed in as a callable so fb_marketplace_sweep never has to
    import this module back."""
    def verify(ctx, rows):
        removed, checked, auth_failed = [], 0, False
        for r in rows:
            iid = r.get("item_id") or ""
            url = r.get("url") or f"https://www.facebook.com/marketplace/item/{iid}"
            status, marker = probe_listing(ctx, url)
            if status == STATUS_UNKNOWN and page is not None:
                status, marker = probe_listing_rendered(page, url)
            checked += 1
            record_verification(con, iid, status)
            if status in (STATUS_SOLD, STATUS_GONE):
                removed.append({**r, "removal": status, "marker": marker})
            elif status == STATUS_AUTH:
                # The session died mid-check. Stop rather than label the rest of
                # the list ambiguous for no reason.
                auth_failed = True
                break
            if on_progress:
                on_progress(checked, len(rows), status, marker)
            pause()
        return removed, checked, auth_failed
    return verify


def record_verification(con, item_id, status):
    definite = status in DEFINITE
    now = iso(now_local())
    con.execute(
        "INSERT INTO listing_state (item_id, first_seen, last_verified, status, "
        "status_confirmed_at, verify_failures) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(item_id) DO UPDATE SET last_verified=excluded.last_verified, "
        # Only a definite answer is allowed to move the status.
        "status=CASE WHEN ?=1 THEN excluded.status ELSE listing_state.status END, "
        "status_confirmed_at=CASE WHEN ?=1 THEN excluded.status_confirmed_at "
        "     ELSE listing_state.status_confirmed_at END, "
        "verify_failures=CASE WHEN ?=1 THEN 0 "
        "     ELSE COALESCE(listing_state.verify_failures,0)+1 END",
        (item_id, now, now, status if definite else STATUS_LIVE, now,
         0 if definite else 1, int(definite), int(definite), int(definite)))
    con.commit()


def mark_seen_in_feed(con, item_ids):
    """Appearing in the feed is proof of life, and it costs nothing."""
    if not item_ids:
        return
    now = iso(now_local())
    con.executemany(
        "INSERT INTO listing_state (item_id, first_seen, last_seen_in_feed, "
        "last_verified, status, status_confirmed_at) VALUES (?,?,?,?,'live',?) "
        "ON CONFLICT(item_id) DO UPDATE SET "
        "last_seen_in_feed=excluded.last_seen_in_feed, "
        "last_verified=excluded.last_verified, status='live', "
        "status_confirmed_at=excluded.status_confirmed_at, verify_failures=0",
        [(i, now, now, now, now) for i in item_ids])
    con.commit()


def mark_described(con, item_ids):
    if not item_ids:
        return
    now = iso(now_local())
    con.executemany(
        "INSERT INTO listing_state (item_id, first_seen, description_fetched_at) "
        "VALUES (?,?,?) ON CONFLICT(item_id) DO UPDATE SET "
        "description_fetched_at=excluded.description_fetched_at",
        [(i, now, now) for i in item_ids])
    con.commit()


def needs_verifying(con, item_ids, interval_hrs):
    """Skip anything already checked within one interval, so a listing that has
    been missing from the feed for weeks isn't re-probed on every run."""
    if not item_ids:
        return []
    cutoff = iso(now_local() - timedelta(hours=max(1.0, interval_hrs)))
    seen = {r[0]: r[1] for r in con.execute(
        "SELECT item_id, last_verified FROM listing_state")}
    return [i for i in item_ids if not seen.get(i) or seen[i] < cutoff]


# ---------- run bookkeeping ----------
def latest_run(con, search_id):
    return con.execute(
        "SELECT run_id, started, finished, total_count FROM search_runs "
        "WHERE search_id=? AND status='ok' ORDER BY run_id DESC LIMIT 1",
        (search_id,)).fetchone()


def previous_item_ids(con, search_id):
    row = latest_run(con, search_id)
    if not row:
        return []
    return [r[0] for r in con.execute(
        "SELECT item_id FROM run_items WHERE run_id=?", (row[0],))]


def first_found_by_item(con, search_id):
    """When *this* search first turned up each listing it has ever seen.

    Per search, and that is the whole point. The archive is shared across every
    search on the machine, so a listing another search found in June is not
    something this one found in June — a date from before the search existed is
    worse than no date at all. run_items already records which listings made up
    each run and search_runs when each run began, so the earliest run of this
    search that carried a listing is the answer, and it can't predate the
    search's first run.

    Returns None until the search has a completed run behind it, which tells the
    sweep to leave the column out altogether: on a first run every listing was
    found just now, and a whole column saying so is noise on every card."""
    prior = con.execute(
        "SELECT count(*) FROM search_runs WHERE search_id=? AND status='ok'",
        (search_id,)).fetchone()[0]
    if not prior:
        return None
    return {r[0]: r[1] for r in con.execute(
        "SELECT ri.item_id, MIN(sr.started) FROM run_items ri "
        "JOIN search_runs sr ON sr.run_id = ri.run_id "
        "WHERE sr.search_id=? AND sr.status='ok' AND COALESCE(sr.started,'') <> '' "
        "GROUP BY ri.item_id", (search_id,))}


def rows_for_ids(con, item_ids, fields):
    out, ids = [], list(item_ids)
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        q = ",".join("?" * len(chunk))
        for row in con.execute(
                f"SELECT {','.join(fields)} FROM listings WHERE item_id IN ({q})",
                chunk):
            out.append(dict(zip(fields, row)))
    return out


def record_run(con, search_id, summary):
    cur = con.execute(
        "INSERT INTO search_runs (search_id, started, finished, duration_seconds, "
        "new_count, total_count, removed_count, status, error) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (search_id, summary.get("started"), summary.get("finished"),
         summary.get("duration_seconds"), len(summary.get("new_ids") or []),
         len(summary.get("total_ids") or []), len(summary.get("removed") or []),
         summary.get("status") or "ok", summary.get("error")))
    run_id = cur.lastrowid
    new = set(summary.get("new_ids") or [])
    con.executemany(
        "INSERT OR REPLACE INTO run_items (run_id, item_id, is_new) VALUES (?,?,?)",
        [(run_id, i, 1 if i in new else 0)
         for i in (summary.get("total_ids") or [])])
    con.commit()
    return run_id


# ---------- the single-run lock ----------
def _pid_alive(pid):
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class AlreadyRunning(Exception):
    def __init__(self, holder):
        what = (holder or {}).get("what", "a run")
        started = (holder or {}).get("started", "an unknown time")
        super().__init__(f"{what} has been running since {started}")
        self.holder = holder or {}


class run_lock:
    """Only one sweep may touch the Facebook session at a time, whether it came
    from the scheduler or from someone pressing Start in the window. Chromium
    would refuse the shared profile anyway; this turns that crash into a clean,
    explained skip."""

    def __init__(self, what="run"):
        self.what = what
        self.fd = None

    def __enter__(self):
        SCHEDULE_DIR.mkdir(parents=True, exist_ok=True)
        for attempt in (1, 2):
            try:
                self.fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, json.dumps(
                    {"pid": os.getpid(), "what": self.what,
                     "started": iso(now_local())}).encode("utf-8"))
                return self
            except FileExistsError:
                holder = self._read_holder()
                if attempt == 1 and self._reclaim(holder):
                    continue
                raise AlreadyRunning(holder)
        raise AlreadyRunning(None)

    @staticmethod
    def _read_holder():
        try:
            return json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _reclaim(holder):
        pid = int(holder.get("pid") or 0)
        started = parse_iso(holder.get("started"))
        stale = (started is None
                 or now_local() - started > timedelta(hours=LOCK_STALE_HOURS))
        if pid and _pid_alive(pid) and not stale:
            return False
        log(f"  clearing a stale lock from pid {pid or '?'} "
            f"(started {holder.get('started') or 'unknown'})")
        try:
            LOCK_PATH.unlink()
        except OSError:
            return False
        return True

    def __exit__(self, *exc):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        # Only remove the lock if it's still ours. A run that outlived the
        # stale threshold may have had its lock reclaimed and replaced by a
        # newer run, and deleting that one would let a third run start.
        if self._read_holder().get("pid") == os.getpid():
            try:
                LOCK_PATH.unlink()
            except OSError:
                pass
        return False


# ---------- the report ----------
def _money(r):
    return (r.get("price") or "").strip() or "no price"


def _plural(n, word, plural=None):
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def _listing_line(r):
    bits = [r.get("title") or "(untitled)", _money(r)]
    where = (r.get("listing_location") or "").strip()
    if where:
        bits.append(where)
    return " — ".join(bits)


def search_queries(search):
    """The queries of a scheduled search, whichever field it carries them in."""
    return listings.query_list(search.get("queries") or search.get("query"))


def searched_for(search):
    """What the search looked for, as a phrase: 'defender 110', or
    'defender 110' or 'land rover 110' when it has more than one query."""
    return " or ".join(f"'{q}'" for q in search_queries(search))


def build_report(search, summary, next_run=None, warnings=()):
    """Plain text and HTML are built from one set of numbers so they can never
    disagree with each other."""
    new_rows = summary.get("new_rows") or []
    removed = summary.get("removed") or []
    total = len(summary.get("total_ids") or [])
    started = parse_iso(summary.get("started"))
    dur = fmt_dur(summary.get("duration_seconds"))
    sold = [r for r in removed if r.get("removal") == STATUS_SOLD]
    gone = [r for r in removed if r.get("removal") != STATUS_SOLD]
    name = search.get("name") or search.get("query") or "Scheduled search"

    subject = f"{name}: {len(new_rows)} new, {total} total"
    if removed:
        subject += f", {len(removed)} gone"

    T = []
    for w in warnings:
        T.append(f"!! {w}")
    if warnings:
        T.append("")
    T.append(name)
    T.append(f"Searched for {searched_for(search)} across "
             f"{_plural(len(search.get('cities') or []), 'city', 'cities')}.")
    T.append(f"Started {fmt_when(started)} and took {dur}.")
    T.append("")
    T.append(f"  {len(new_rows)} new listing{'' if len(new_rows) == 1 else 's'}")
    T.append(f"  {total} total listing{'' if total == 1 else 's'} being tracked")
    T.append(f"  {len(removed)} sold or taken down since the last run")
    if summary.get("descriptions_fetched") is not None:
        n = summary["descriptions_fetched"]
        T.append(f"  {n} description{'' if n == 1 else 's'} fetched "
                 f"(only new listings need one)")
    T.append("")

    for label, rows in (("NEW", new_rows), ("SOLD", sold), ("TAKEN DOWN", gone)):
        if not rows:
            continue
        T.append(f"{label} ({len(rows)})")
        for r in rows:
            T.append(f"  - {_listing_line(r)}")
            T.append(f"    {r.get('url') or ''}")
        T.append("")
    if not removed:
        T.append("Nothing was sold or taken down since the last run.")
        T.append("")

    per_city = summary.get("per_city") or {}
    if per_city:
        T.append("Per city this run:")
        for label, st in per_city.items():
            T.append(f"  {label}: {st.get('kept', 0)} kept of "
                     f"{st.get('cards', 0)} seen")
        T.append("")

    T.append("The attached files contain stripped-down versions of the listings "
             "in your search results. To view the full gallery, open Faceplace "
             "Marketbook on your computer and go to the Past searches tab.")
    url = _gallery_url(summary.get("gallery"))
    if url:
        T.append(url)
        T.append("(This link only works on the computer that ran the search.)")
    T.append("")
    if next_run:
        T.append(f"Next run: {fmt_when(next_run)}.")
    T.append("To pause or change this search, open Faceplace Marketbook and go to "
             "the Scheduled searches tab.")

    html = _report_html(name, search, summary, new_rows, sold, gone, total, dur,
                        started, next_run, warnings)
    return subject, "\n".join(T), html


def _esc(s):
    return (str(s if s is not None else "").replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def _gallery_url(path):
    """An http://127.0.0.1 link to a gallery on this computer.

    file:// looks like a link in the HTML we write and like ordinary text in
    Gmail, which strips that scheme before the message is shown. A localhost
    http URL survives, and only does anything on this machine. None rather than
    a guess if the path isn't absolute: a link to the wrong place is worse than
    no link.
    """
    try:
        p = Path(path)
        if not p.is_absolute():
            return None
        return (f"http://{GALLERY_HOST}:{GALLERY_PORT}/"
                f"{quote(p.as_posix().lstrip('/'), safe=':/')}")
    except (TypeError, ValueError, OSError):
        return None


def resolve_gallery(url_path):
    """The local file a gallery URL is asking for, or None if it isn't one we
    should be serving. Only gallery.html files under the runs folder — a
    localhost server that would read anywhere on the disk is a hole."""
    raw = unquote(url_path or "")
    if re.match(r"^/[A-Za-z]:", raw):
        raw = raw[1:]
    try:
        wanted = Path(raw).resolve()
        root = Path(RUNS_DIR).resolve()
        wanted.relative_to(root)
    except (TypeError, ValueError, OSError):
        return None
    if wanted.name not in GALLERY_NAMES or not wanted.is_file():
        return None
    return wanted


class _GalleryHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        gallery = resolve_gallery(urlparse(self.path).path)
        if not gallery:
            self.send_error(404, "Not found")
            return
        data = gallery.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        return


def _gallery_server_up():
    try:
        with socket.create_connection((GALLERY_HOST, GALLERY_PORT), timeout=0.3):
            return True
    except OSError:
        return False


def ensure_gallery_server():
    """Start the localhost gallery server if it isn't already answering.

    The process outlives the tick that launched it, so a report emailed at 5am
    still has something listening when the link is clicked that evening. Bind
    failures (already running, or the port taken) are silent: the email still
    names Past searches and carries the attachments."""
    if _gallery_server_up():
        return
    kw = dict(cwd=str(ROOT), stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL)
    if os_name() == "windows":
        flags = 0
        for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP",
                     "CREATE_NO_WINDOW"):
            flags |= getattr(subprocess, name, 0)
        kw["creationflags"] = flags
    else:
        kw["start_new_session"] = True
    try:
        subprocess.Popen(
            [python_exe(), str(paths.SCHEDULER_ENTRY), "--serve-galleries"],
            **kw)
    except OSError:
        pass


def serve_galleries():
    try:
        httpd = ThreadingHTTPServer((GALLERY_HOST, GALLERY_PORT), _GalleryHandler)
    except OSError:
        return
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


def _gallery_html(summary, accent):
    """The way back to the full-size gallery.

    It lives on one computer, so the link to it is a localhost http one, and
    that only does anything on that machine — clicked from a phone it lands on
    the browser's own connection error. So the link is never the only way
    back: the attachments open anywhere, and on the machine that ran the search
    the app lists the run itself, which is a route that survives not having a
    path to hand.
    """
    foot = "color:#6b6b5e;font-size:13px;margin:0 0 8px"
    attached = ("The attached files contain stripped-down versions of the "
                "listings in your search results. To view the full gallery, "
                "open Faceplace Marketbook on your computer and go to the "
                "<b>Past searches</b> tab")
    url = _gallery_url(summary.get("gallery"))
    if not url:
        return f'<p style="{foot}">{attached}.</p>'
    return (f'<p style="{foot}">{attached}, or click the link below.</p>'
            f'<p style="margin:0 0 4px"><a href="{_esc(url)}" '
            f'style="color:{accent};font-weight:700;text-decoration:underline">'
            f'Open the full gallery &rarr;</a></p>'
            f'<p style="color:{accent};font-size:13px;margin:0 0 12px">'
            f'Note: the link will only work if you&rsquo;re on the computer '
            f'that ran the search.</p>')


def _searched_for_html(search):
    return " or ".join(f"&lsquo;{_esc(q)}&rsquo;" for q in search_queries(search))


def _rows_html(rows, accent):
    out = []
    for r in rows:
        where = (r.get("listing_location") or "").strip()
        out.append(
            f'<li style="margin:0 0 10px">'
            f'<a href="{_esc(r.get("url"))}" style="color:{accent};font-weight:700;'
            f'text-decoration:none">{_esc(r.get("title") or "(untitled)")}</a><br>'
            f'<span style="color:#6b6b5e">{_esc(_money(r))}'
            + (f" &middot; {_esc(where)}" if where else "")
            + "</span></li>")
    return "".join(out)


def _report_html(name, search, summary, new_rows, sold, gone, total, dur, started,
                 next_run, warnings):
    olive, accent, ink = "#3c4033", "#8a6f3b", "#22221e"
    warn_html = "".join(
        f'<p style="background:#f7e9d0;border-left:4px solid #a8791f;'
        f'padding:10px 14px;margin:0 0 14px;color:#5a4310">'
        f'<b>Heads up:</b> {_esc(w)}</p>' for w in warnings)
    stat_html = "".join(
        f'<td style="padding:0 22px 0 0"><div style="font:700 26px/1.1 Helvetica,'
        f'Arial,sans-serif;color:{ink}">{n}</div>'
        f'<div style="color:#6b6b5e;font-size:13px">{_esc(lab)}</div></td>'
        for n, lab in ((len(new_rows), "new"), (total, "tracked"),
                       (len(sold) + len(gone), "sold or taken down")))

    sections = ""
    for title, rows in (("New listings", new_rows), (f"Sold ({len(sold)})", sold),
                        (f"Taken down ({len(gone)})", gone)):
        if rows:
            sections += (f'<h3 style="color:{olive};margin:24px 0 8px">'
                         f'{_esc(title)}</h3>'
                         f'<ul style="padding-left:18px;margin:0">'
                         f'{_rows_html(rows, accent)}</ul>')
    if not sold and not gone:
        sections += ('<p style="color:#6b6b5e;margin:24px 0 0">Nothing was sold or '
                     'taken down since the last run.</p>')
    next_html = (f'<p style="color:#6b6b5e;font-size:13px;margin:0 0 8px">Next run: '
                 f'{_esc(fmt_when(next_run))}.</p>' if next_run else "")

    return f"""<!doctype html>
<html><body style="margin:0;background:#f4f3ec;padding:24px;
 font:15px/1.55 Helvetica,Arial,sans-serif;color:{ink}">
<div style="max-width:640px;margin:0 auto;background:#fff;border-radius:10px;
 padding:28px 30px">
{warn_html}
<h1 style="font:700 22px/1.2 Helvetica,Arial,sans-serif;color:{olive};
 margin:0 0 4px">{_esc(name)}</h1>
<p style="color:#6b6b5e;margin:0 0 20px">{_searched_for_html(search)}
across {_plural(len(search.get('cities') or []), 'city', 'cities')} &middot; started
{_esc(fmt_when(started))} &middot; took {_esc(dur)}</p>
<table style="border-collapse:collapse;margin:0 0 8px"><tr>{stat_html}</tr></table>
{sections}
<hr style="border:0;border-top:1px solid #e4e2d6;margin:26px 0 16px">
{_gallery_html(summary, accent)}
{next_html}
<p style="color:#6b6b5e;font-size:13px;margin:0">To pause or change this search,
open Faceplace Marketbook and go to the <b>Scheduled searches</b> tab.</p>
</div></body></html>"""


# ---------- attachments ----------
def build_attachments(csv_path, new_ids, out_dir=None):
    """Two stripped-down galleries: just this run's new listings, and everything
    currently tracked. Neither carries photos. Thumbnails are the whole reason a
    report can grow big enough for a mail server to refuse it, and the gallery
    with the photos in it is already on the computer that ran the search."""
    import build_gallery
    csv_path = Path(csv_path)
    out_dir = Path(out_dir) if out_dir else csv_path.parent
    built = []
    for name, ids in (("new-listings.html", set(new_ids)),
                      ("all-results.html", None)):
        if ids is not None and not ids:
            continue
        path = out_dir / name
        # The new-listings gallery is every listing this run found, so a
        # first-found date on it is this run's start on every single card.
        build_gallery.build(csv_path, path, images=False, only_ids=ids,
                            quiet=True, dates=ids is None)
        built.append({"name": name, "path": path, "ids": ids,
                      "size": path.stat().st_size})
    return [(b["name"], b["path"].read_bytes(), "html") for b in built], built


# ---------- failure notices ----------
REAUTH_STEPS = """What to do:

Open the Faceplace Marketbook folder on your computer and double-click "Log into
Facebook" — the .command file on a Mac, the .bat file on Windows. It opens
Facebook in a browser window. Log into Facebook the way you normally would,
including any two-factor code and captcha. You can close the window once you can
see your normal Facebook feed, and your next scheduled run will work as usual."""


def notify_failure(cfg, to, search, kind, detail, next_run=None):
    """Email sent when a run couldn't happen. Always says what to do next, since
    an error you can't act on is just noise in your inbox."""
    name = (search or {}).get("name") or "Faceplace Marketbook"
    if kind == "session_expired":
        subject = f"{name}: please log into Facebook again"
        body = [f"The scheduled run of '{name}' stopped because the saved "
                f"Facebook session is no longer valid.", "", REAUTH_STEPS]
    else:
        subject = f"{name}: the scheduled run failed"
        body = [f"The scheduled run of '{name}' hit an unexpected error and "
                f"stopped. The search is still scheduled, so it will try again "
                f"on its normal interval.", "",
                "If it keeps failing, open Faceplace Marketbook and run the "
                "search by hand — the error usually shows up more clearly "
                "there. The details below are what the run recorded.", "",
                "Details:", str(detail or "").strip()]
    if next_run:
        body += ["", f"Next attempt: {fmt_when(next_run)}."]
    body += ["", "To pause this search, open Faceplace Marketbook and go to the "
                 "Scheduled searches tab."]
    text = "\n".join(body)
    try:
        send_email(cfg, to, subject, text)
        log(f"  emailed the {kind.replace('_', ' ')} notice")
    except Exception as e:
        log(f"  couldn't send the {kind} email ({e})")


# ---------- running one scheduled search ----------
def archive_previous(run_dir):
    """Keep the numbered history of run.json and the emailed report, so the
    folder itself only ever holds the current results."""
    run_json = run_dir / "run.json"
    if not run_json.exists():
        return
    hist = run_dir / "history"
    hist.mkdir(exist_ok=True)
    n = 1 + max([0] + [int(m.group(1)) for m in
                       (re.match(r"run-(\d+)\.json$", p.name) for p in hist.iterdir())
                       if m])
    run_json.replace(hist / f"run-{n}.json")
    for name in ("report.html", "new-listings.html", "all-results.html"):
        p = run_dir / name
        if p.exists():
            p.replace(hist / f"{Path(name).stem}-{n}.html")


def run_saved_search(search, email_cfg=None, sweep=None, send=True, now=None,
                     forced=False):
    """One scheduled run, start to finish. `sweep` is injectable so the tests can
    exercise everything around the browser without opening one. `forced` means
    someone asked for this run by hand, so being past the scheduled time is
    expected and shouldn't be reported as the machine having been asleep."""
    import fb_marketplace_sweep as fb

    email_cfg = email_cfg if email_cfg is not None else load_email_config()
    to = (search.get("email_to") or "").strip() or email_cfg.get("address")
    started_at = now or now_local()
    late = 0.0 if forced else lateness_hours(search, started_at)
    warnings = []

    searches = load_searches()
    rec = find_search(searches, search.get("id")) or search
    rec["last_started"] = iso(started_at)
    rec["next_run"] = iso(next_run_at(rec, started_at))
    save_searches(searches)

    con = storage.open_db(storage.DB_PATH)
    try:
        prev_ids = previous_item_ids(con, search["id"])
        interval_hrs = interval_hours(search.get("interval") or {})
        stale = set(needs_verifying(con, prev_ids, interval_hrs))
        prev_rows = rows_for_ids(con, prev_ids, storage.FIELDS)
        first_found = first_found_by_item(con, search["id"])
        # Anything checked within the last interval is carried forward without
        # being probed again; only the rest is handed to the verifier.
        for r in prev_rows:
            r["_recheck"] = r["item_id"] in stale

        run_dir = storage.saved_run_dir(search["name"])
        archive_previous(run_dir)

        def pause():
            lo, hi = descriptions.PACES.get(search.get("pace") or "fast", (1.0, 2.5))
            browser.human_pause(lo, hi)

        verifier = make_verifier(
            con, pause,
            on_progress=lambda i, n, st, mk: log(f"    [{i}/{n}] {st}: {mk}"))

        def verify_only_stale(ctx, rows):
            due = [r for r in rows if r.get("_recheck")]
            skipped = len(rows) - len(due)
            if skipped:
                log(f"  skipping {skipped} recently-checked listing(s)")
            return verifier(ctx, due)

        queries = search_queries(search)
        log(f"Running '{search['name']}' ({searched_for(search)}, "
            f"{len(search.get('cities') or [])} cities)"
            + (f" — {late:.1f}h late" if late >= LATE_AFTER_HOURS else ""))

        runner = sweep or fb.run
        summary = runner(
            queries, fb.DEFAULT_SCROLLS, bool(search.get("exact")),
            thumbs_dir=fb.THUMBS_DIRNAME,
            do_descriptions=bool(search.get("do_descriptions", True)),
            do_thumbs=bool(search.get("do_thumbs", True)),
            do_gallery=True, pace=search.get("pace") or "fast",
            exclude=[t.strip() for t in (search.get("exclude") or "").split(",")
                     if t.strip()],
            min_price=search.get("min_price"), max_price=search.get("max_price"),
            min_year=search.get("min_year"), max_year=search.get("max_year"),
            include_no_year=search.get("include_no_year", True),
            radius_miles=search.get("radius_miles"),
            limit=search.get("limit"),
            only_labels=search.get("cities"), open_gallery=False,
            run_dir=run_dir, previous_rows=prev_rows, first_found=first_found,
            describe_new_only=True,
            verifier=verify_only_stale, login_wait=60, unattended=True)

        if not summary or summary.get("status") != "ok":
            raise RuntimeError((summary or {}).get("error") or "the sweep failed")

        # feed_ids, not total_ids: totals include listings that were only
        # carried forward from earlier runs. Stamping those "seen alive" would
        # refresh their last_verified every run, so a listing that had actually
        # sold would never come due for re-checking and never be dropped.
        mark_seen_in_feed(con, summary.get("feed_ids") or [])
        mark_described(con, summary.get("described_ids") or [])
        record_run(con, search["id"], summary)

        if late >= LATE_AFTER_HOURS:
            warnings.append(
                f"This run started {late:.0f} hours later than scheduled, which "
                f"usually means the computer was asleep or switched off at the "
                f"time. Nothing was lost — it ran as soon as the machine was "
                f"available again.")
        km = summary.get("radius_km")
        want = summary.get("radius_requested_miles")
        got = summary.get("radius_miles")
        if want and got != want:
            # The one failure this feature can produce that looks like success.
            # Facebook's radius control is a custom widget with no stable
            # selectors, so when it moves, the run still finishes and still
            # emails results — they are just from the wrong distance, and
            # nothing but this line would say so.
            warnings.append(
                f"This search asks for a {want}-mile radius, but Facebook's "
                f"radius control didn't take it. The run searched "
                f"{f'{got} miles' if got else 'whatever radius was already set'} "
                f"around each city instead, so these results cover the wrong "
                f"distance. Facebook rearranges that control from time to time; "
                f"if this keeps happening, please reach out so we can fix it.")
        elif not want and km and km < fb.EXPECTED_RADIUS_KM:
            warnings.append(
                f"Your Marketplace search radius is set to about "
                f"{round(km / 1.609)} miles, rather than the 500 Facebook "
                f"allows, so this run only looked that far out from each city. "
                f"If you wanted a wider net, log in to your Facebook account "
                f"on your computer and raise the radius in Marketplace.")
        if summary.get("interrupted"):
            warnings.append(
                "The run was stopped partway through, so some of the cities "
                "were never searched."
                if summary.get("interrupted_during") == "sweep" else
                "The run was stopped partway through, so some descriptions and "
                "photos are missing.")
        wakes = wake_queue_state()
        if wakes.get("relevant") and wakes.get("nag"):
            # The reports are the one channel guaranteed to reach someone whose
            # machine runs unattended, so this is where the queue running dry
            # gets announced before it happens rather than after.
            days = wakes.get("days_left") or 0
            warnings.append(
                ("The wake-ups that let this Mac wake itself for runs on an "
                 "hours interval cover only the next "
                 f"{days} day{'' if days == 1 else 's'}. "
                 if days else
                 "The wake-ups that let this Mac wake itself for runs on an "
                 "hours interval have run out. ")
                + "Open Faceplace Marketbook and renew them from the Email & "
                  "Setup tab. Until then, hours-interval searches run once a "
                  f"day at {DAILY_HOUR}am, and whenever the Mac is awake.")
        unknown = summary.get("unknown_cities") or []
        if unknown:
            # Silence here would be the worst outcome: the report would look
            # complete while a whole region went unsearched every single run.
            names = ", ".join(c.get("label") or "?" for c in unknown)
            warnings.append(
                f"Facebook doesn't recognise {'this city' if len(unknown) == 1 else 'these cities'}, "
                f"so nothing was searched for {'it' if len(unknown) == 1 else 'them'}: {names}. "
                f"Open Faceplace Marketbook, remove {'it' if len(unknown) == 1 else 'them'} "
                f"from the city list, and add {'it' if len(unknown) == 1 else 'them'} again by "
                f"pasting a Marketplace address from your browser.")

        rec["last_finished"] = iso(now_local())
        searches = load_searches()
        live = find_search(searches, search["id"])
        if live:
            live["last_started"] = rec["last_started"]
            live["last_finished"] = rec["last_finished"]
            live["next_run"] = rec["next_run"]
            save_searches(searches)

        next_run = parse_iso(rec.get("next_run"))
        subject, text, html = build_report(search, summary, next_run, warnings)
        (Path(summary["run_dir"]) / "report.html").write_text(html, encoding="utf-8")

        if send and email_ready(email_cfg) and to:
            attachments, built = build_attachments(
                summary["csv"], summary.get("new_ids") or [],
                out_dir=summary["run_dir"])
            for b in built:
                log(f"  {b['name']}: {b['size'] / 1e6:.1f} MB (no photos)")
            msg_id = send_email(email_cfg, to, subject, text, html, attachments)
            log(f"  emailed {to}")
            summary["message_id"] = msg_id
        elif send:
            log("  email isn't set up, so no report was sent")
        summary["report"] = {"subject": subject, "text": text, "html": html,
                             "warnings": warnings}
        return summary
    finally:
        con.close()


def tick(now=None, sweep=None, send=True, force=None):
    """What the OS calls. Runs every due search, one at a time, in the order they
    were created. Holds the lock for the whole batch so a manual run can't start
    halfway through."""
    ensure_gallery_server()
    from fb_marketplace_sweep import SessionExpired

    now = now or now_local()
    searches = load_searches()
    if force:
        rec = find_search(searches, force)
        if not rec:
            raise SystemExit(f"No scheduled search called '{force}'.")
        queue = [rec]
    else:
        queue = due_searches(searches, now)
    if not queue:
        return []

    email_cfg = load_email_config()
    results = []
    try:
        with run_lock("scheduled run"):
            log(f"{len(queue)} search{'' if len(queue) == 1 else 'es'} to run: "
                + ", ".join(s["name"] for s in queue))
            for i, search in enumerate(queue):
                to = ((search.get("email_to") or "").strip()
                      or email_cfg.get("address"))
                try:
                    results.append(run_saved_search(
                        search, email_cfg, sweep=sweep, send=send, now=now,
                        forced=bool(force)))
                except SessionExpired as e:
                    # Every remaining search would fail at the same wall, so stop
                    # and send one email instead of one per search.
                    log(f"  session expired: {e}")
                    notify_failure(email_cfg, to, search, "session_expired", e,
                                   parse_iso(search.get("next_run")))
                    skipped = [s["name"] for s in queue[i + 1:]]
                    if skipped:
                        log(f"  skipping {', '.join(skipped)} — same session")
                    break
                except Exception as e:
                    log(f"  failed: {e}")
                    log(traceback.format_exc())
                    notify_failure(email_cfg, to, search, "error",
                                   traceback.format_exc(limit=4),
                                   parse_iso(search.get("next_run")))
    except AlreadyRunning as e:
        log(f"Skipping this tick — {e}.")
        return []
    # The tick runs unprivileged, so it can read how much of the wake queue is
    # left but never refill it; the reports carry the nag, and this line makes
    # the same fact visible to anyone reading the log.
    state = wake_queue_state()
    if state.get("relevant") and state.get("nag"):
        log(f"  wake-up queue is nearly empty ({state['days_left']} days left) — "
            f"open the settings window to renew it")
    return results


# ---------- letting the OS wake the machine and call us ----------
MAC_LABEL = "com.faceplace.marketbook.scheduler"
WIN_TASK = "FaceplaceMarketbook"


def python_exe():
    """The venv interpreter, so a scheduled run gets the same Playwright the
    launcher installed rather than whatever system Python the OS hands us."""
    for cand in (paths.VENV_DIR / "bin" / "python3",
                 paths.VENV_DIR / "bin" / "python",
                 paths.VENV_DIR / "Scripts" / "pythonw.exe",
                 paths.VENV_DIR / "Scripts" / "python.exe"):
        if cand.exists():
            return str(cand)
    return sys.executable


def plist_path():
    return Path.home() / "Library" / "LaunchAgents" / f"{MAC_LABEL}.plist"


def mac_plist():
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{MAC_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python_exe()}</string>
    <string>{paths.SCHEDULER_ENTRY}</string>
    <string>--tick</string>
  </array>
  <key>WorkingDirectory</key><string>{ROOT}</string>
  <key>StartInterval</key><integer>{TICK_SECONDS}</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{AGENT_LOG}</string>
  <key>StandardErrorPath</key><string>{AGENT_LOG}</string>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
"""


def os_name():
    return {"Darwin": "darwin", "Windows": "windows"}.get(platform.system(), "other")


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def earliest_next_run():
    times = [parse_iso(s.get("next_run")) for s in load_searches()
             if s.get("enabled")]
    times = [t for t in times if t]
    return min(times) if times else None


# ---------- the wake queue ----------
# Hour-interval searches need the Mac woken several times a day, and macOS has
# no standing rule for that: `pmset repeat` holds exactly one wake per day (the
# 5am one), and everything else has to be a dated one-off event. Writing one
# takes root, so events are queued WAKE_HORIZON_DAYS deep in a single authorized
# batch — during the same password prompt as turning automatic runs on, or when
# a search that changes the grid is saved — and renewed the same way. If the
# queue runs dry, nothing breaks: the 5am wake never expires, so hourly searches
# fall back to once a day plus whenever the machine is awake anyway.
#
# Each event carries WAKE_OWNER, which is pmset's own mechanism for programs
# sharing the schedule: it makes ours recognisable in `pmset -g sched`, lets a
# cancel remove only ours, and leaves everyone else's alone.

def wake_hours_needed(searches=None):
    """The union of every enabled hour-interval search's grid, as hours of the
    day — minus DAILY_HOUR, which the standing 5am repeat already covers."""
    searches = load_searches() if searches is None else searches
    hours = set()
    for s in searches:
        iv = s.get("interval") or {}
        if s.get("enabled") and iv.get("unit") == "hours":
            hours.update(grid_hours(iv.get("every")))
    hours.discard(DAILY_HOUR)
    return sorted(hours)


def wake_times_needed(now=None, searches=None):
    """Every wake-up the queue should hold, as datetimes, oldest first."""
    now = now or now_local()
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    out = [day + timedelta(days=d, hours=h)
           for d in range(WAKE_HORIZON_DAYS + 1)
           for h in wake_hours_needed(searches)]
    return [t for t in out if t > now][:WAKE_MAX_EVENTS]


_WAKE_LINE = re.compile(r"wake at (\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}) by '([^']*)'")


def scheduled_wakes():
    """The app's own wake-ups currently queued with the system. Reading the
    schedule needs no privileges; only writing it does."""
    if os_name() != "darwin":
        return []
    r = _run(["pmset", "-g", "sched"])
    out = []
    for m in _WAKE_LINE.finditer(r.stdout or ""):
        if m.group(2) == WAKE_OWNER:
            try:
                out.append(datetime.strptime(m.group(1), "%m/%d/%Y %H:%M:%S"))
            except ValueError:
                pass
    return sorted(out)


def wake_queue_state(now=None):
    """What's left of the queue, for the settings window and the reports.

    `relevant` is False anywhere the queue means nothing: on Windows the
    scheduled task wakes the machine itself, and with no hour-interval searches
    the 5am repeat is the whole schedule."""
    if os_name() != "darwin" or not wake_hours_needed():
        return {"relevant": False}
    now = now or now_local()
    left = [w for w in scheduled_wakes() if w > now]
    until = left[-1] if left else None
    days_left = max(0, (until - now).days) if until else 0
    return {"relevant": True, "queued": len(left), "days_left": days_left,
            "until": iso(until),
            "renew": days_left < WAKE_RENEW_BELOW_DAYS,
            "nag": days_left <= WAKE_NAG_BELOW_DAYS}


def _cancel_lines():
    """The pmset commands that drop every queued event of ours. Best-effort
    (`|| true`): a stale event that dodges its cancel is one extra wake-up that
    expires on its own, not a reason to abort the batch."""
    return [f"pmset schedule cancel wake '{w:%m/%d/%y %H:%M:%S}' "
            f"'{WAKE_OWNER}' || true" for w in scheduled_wakes()]


def _wake_lines(now=None, searches=None):
    """The pmset commands that make the system queue match the saved searches:
    drop every event of ours, then write the fresh set."""
    return _cancel_lines() + [
        f"pmset schedule wake '{w:%m/%d/%y %H:%M:%S}' '{WAKE_OWNER}'"
        for w in wake_times_needed(now, searches)]


def _admin_shell(lines):
    """Run shell lines as root, in one authorization.

    The lines go into a script file rather than an inline string because a
    hundred chained pmset calls through AppleScript's quoting is how shell
    injection bugs get written. `sudo -n` goes first — free when credentials
    happen to be cached — and otherwise osascript puts up the standard macOS
    administrator prompt, once, for the whole batch."""
    if not lines:
        return True
    SCHEDULE_DIR.mkdir(parents=True, exist_ok=True)
    script = SCHEDULE_DIR / "power_schedule.sh"
    script.write_text("#!/bin/sh\n" + "\n".join(lines) + "\n", encoding="utf-8")
    if _run(["sudo", "-n", "/bin/sh", str(script)]).returncode == 0:
        return True
    r = _run(["osascript", "-e",
              'on run argv',
              '-e', 'do shell script "/bin/sh " & quoted form of item 1 of argv '
                    'with administrator privileges',
              '-e', 'end run', str(script)])
    return r.returncode == 0


def renew_wakes(now=None, force=False):
    """Bring the wake queue back to full depth, prompting for a password if
    needed. Returns (changed, note): `changed` says whether anything was
    written, and `note` is the user-facing sentence when there's something to
    say — a refusal, or what the new queue covers.

    Without `force`, a queue that's still deep enough and covers the right
    hours is left alone, so saving a search doesn't prompt for a password it
    doesn't need."""
    if os_name() != "darwin":
        return False, None
    now = now or now_local()
    needed = wake_hours_needed()
    have = [w for w in scheduled_wakes() if w > now]
    if not needed:
        if not have:
            return False, None
        # The last hourly search is gone; the leftovers get cleaned up, but
        # they'd also expire by themselves, so a declined prompt costs nothing.
        if _admin_shell(_wake_lines(now)):
            return True, None
        return False, None
    fresh = (bool(have)
             and sorted({w.hour for w in have}) == needed
             and (have[-1] - now).days >= WAKE_RENEW_BELOW_DAYS)
    if fresh and not force:
        return False, None
    if not _admin_shell(_wake_lines(now)):
        return False, ("The wake-up schedule wasn't updated. You can renew the "
                       "wake-ups any time from the Email & Setup tab.")
    state = wake_queue_state(now)
    if not state.get("until"):
        return True, None
    return True, (f"Your Mac will wake itself for runs that are scheduled on "
                  f"hours intervals through "
                  f"{parse_iso(state['until']):%A, %B %-d} — "
                  f"{state['days_left']} days out. Opening this window now and "
                  f"then keeps that topped up.")


def in_protected_folder(path=None):
    """Whether macOS will hide this folder from a background task."""
    if os_name() != "darwin":
        return None
    parts = (path or ROOT).parts
    home = Path.home().parts
    if parts[:len(home)] != home or len(parts) <= len(home):
        return None
    return parts[len(home)] if parts[len(home)] in MAC_PROTECTED else None


def permission_help():
    """What to do about a Mac that started the scheduler and then denied it every
    file. Both fixes work; moving the folder needs no password."""
    folder = in_protected_folder()
    suggestion = Path.home() / ROOT.name
    lines = []
    if folder:
        lines.append(
            f"macOS won't let a scheduled task read anything in your {folder} "
            f"folder, and that's where this app lives. Automatic runs are set up "
            f"but can't do anything yet.")
    else:
        lines.append("The scheduler started but couldn't reach this folder, so "
                     "automatic runs won't do anything yet.")
    lines.append(
        f"The simplest fix: move this whole folder somewhere macOS doesn't guard, "
        f"such as {suggestion}. Drag it there in Finder, open it, start Faceplace "
        f"Marketbook again, and turn automatic runs back on.")
    lines.append(
        f"If you'd rather leave it where it is: open System Settings > Privacy & "
        f"Security > Full Disk Access, click +, press Command-Shift-G, paste\n"
        f"    {python_exe()}\n"
        f"and switch it on. Nothing else needs doing — the schedule is already "
        f"installed and starts working the moment access is granted.")
    return lines


def verify_agent_can_run(timeout=25):
    """Wait for the freshly installed agent to check in.

    macOS happily installs an agent that can never work: launchd starts the
    interpreter, the system then denies it every file in this folder, and the
    install looks perfect while no search ever runs. Silence is the only symptom,
    so wait for proof of life instead of assuming it."""
    before = last_check_in() or {}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.5)
        now = last_check_in()
        if now and now.get("at") != before.get("at"):
            return True, now
        if now and now.get("pid") != before.get("pid"):
            return True, now
    return False, None


def set_wakes(now=None):
    """Set the standing {DAILY_HOUR}am wake and the whole wake queue, in one
    authorization, and say what happened.

    wakeorpoweron can even start a Mac that was shut down, as long as someone
    unlocks it afterwards when FileVault is on."""
    lines = [f"pmset repeat wakeorpoweron MTWRFSU {DAILY_HOUR:02d}:00:00"]
    lines += _wake_lines(now)
    if not _admin_shell(lines):
        return (f"Couldn't set the wake-ups — the password prompt was cancelled "
                f"or failed. Runs still happen, but only while the Mac is awake. "
                f"To set the daily {DAILY_HOUR}am wake yourself, open Terminal "
                f"and run:\n"
                f"    sudo pmset repeat wakeorpoweron MTWRFSU "
                f"{DAILY_HOUR:02d}:00:00")
    msg = "Your Mac will wake itself for searches that are scheduled for multiple times a day"
    state = wake_queue_state(now)
    if state.get("relevant") and state.get("until"):
        msg += f" until {parse_iso(state['until']):%A, %B %-d}."
    else:
        msg += "."
    return msg


def install_schedule(daily_wake=True, verify=True):
    """Returns (ok, messages). Never raises: a half-installed schedule that
    explains itself is more useful than a traceback."""
    msgs = []
    SCHEDULE_DIR.mkdir(parents=True, exist_ok=True)
    system = os_name()
    if system == "darwin":
        # launchd opens AGENT_LOG before the agent process starts, so its
        # folder has to exist now — check_in() creates it too late for that.
        SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
        p = plist_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(mac_plist(), encoding="utf-8")
        uid = os.getuid()
        _run(["launchctl", "bootout", f"gui/{uid}/{MAC_LABEL}"])
        # Cleared so the check-in that RunAtLoad triggers can't be confused with
        # one from an earlier install.
        HEARTBEAT_PATH.unlink(missing_ok=True)
        r = _run(["launchctl", "bootstrap", f"gui/{uid}", str(p)])
        if r.returncode != 0:
            r = _run(["launchctl", "load", "-w", str(p)])
        if r.returncode != 0:
            return False, [f"macOS refused to start the scheduler: "
                           f"{r.stderr.strip() or r.stdout.strip()}"]
        if verify:
            reachable, _ = verify_agent_can_run()
            if not reachable:
                return False, permission_help()
        msgs.append(f"Scheduler installed; it checks for due searches every "
                    f"{TICK_SECONDS // 60} minutes while awake.")
        ensure_gallery_server()
        if daily_wake:
            msgs.append(set_wakes())
        return True, msgs

    if system == "windows":
        xml = win_task_xml()
        tmp = SCHEDULE_DIR / "task.xml"
        # Task Scheduler insists on UTF-16 for imported XML.
        tmp.write_text(xml, encoding="utf-16")
        r = _run(["schtasks", "/create", "/tn", WIN_TASK, "/xml", str(tmp), "/f"])
        if r.returncode != 0:
            return False, [f"Windows refused to create the task: "
                           f"{(r.stderr or r.stdout).strip()}"]
        msgs.append(f"Scheduled task '{WIN_TASK}' created; it checks for due "
                    f"searches every {TICK_SECONDS // 60} minutes and may wake "
                    f"the computer.")
        ensure_gallery_server()
        return True, msgs

    return False, ["Automatic runs are only set up for macOS and Windows."]


def win_task_xml():
    exe = python_exe()
    start = (now_local() + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%S")
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Runs Faceplace Marketbook scheduled searches when they are due.</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Repetition>
        <Interval>PT{TICK_SECONDS // 60}M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <WakeToRun>true</WakeToRun>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <!-- Generous on purpose: the first run of a wide search with descriptions
         is an overnight job, and Windows kills the process outright at this
         limit - no report, no failure email. The stale-lock cleanup handles a
         genuinely hung run. -->
    <ExecutionTimeLimit>PT12H</ExecutionTimeLimit>
    <StartWhenAvailable>true</StartWhenAvailable>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{exe}</Command>
      <Arguments>"{paths.SCHEDULER_ENTRY}" --tick</Arguments>
      <WorkingDirectory>{ROOT}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def uninstall_schedule():
    msgs = []
    system = os_name()
    if system == "darwin":
        _run(["launchctl", "bootout", f"gui/{os.getuid()}/{MAC_LABEL}"])
        _run(["launchctl", "unload", str(plist_path())])
        plist_path().unlink(missing_ok=True)
        msgs.append("Automatic runs are off. Your scheduled searches are untouched — "
                    "you can still run them by hand.")
        if not _admin_shell(["pmset repeat cancel"] + _cancel_lines()):
            msgs.append(f"The wake-ups couldn't be removed without your "
                        f"password. They're harmless on their own, and the "
                        f"hours-interval ones expire by themselves within "
                        f"{WAKE_HORIZON_DAYS} days; to remove the daily "
                        f"{DAILY_HOUR}am one, open Terminal and run:\n"
                        f"    sudo pmset repeat cancel")
        return True, msgs
    if system == "windows":
        _run(["schtasks", "/delete", "/tn", WIN_TASK, "/f"])
        msgs.append("Automatic runs are off. Your scheduled searches are "
                    "untouched — you can still run them by hand.")
        return True, msgs
    return False, ["Nothing to uninstall on this platform."]


def schedule_installed():
    system = os_name()
    if system == "darwin":
        if not plist_path().exists():
            return False
        r = _run(["launchctl", "list", MAC_LABEL])
        return r.returncode == 0
    if system == "windows":
        return _run(["schtasks", "/query", "/tn", WIN_TASK]).returncode == 0
    return False


def schedule_points_here():
    """Whether the installed schedule still refers to this copy of the project.
    Moving or renaming the folder leaves a task pointing at nothing."""
    system = os_name()
    target = str(paths.SCHEDULER_ENTRY)
    if system == "darwin":
        try:
            return target in plist_path().read_text(encoding="utf-8")
        except OSError:
            return False
    if system == "windows":
        r = _run(["schtasks", "/query", "/tn", WIN_TASK, "/xml"])
        return target in (r.stdout or "")
    return True


def scheduling_available():
    """Whether a scheduled search saved right now would ever actually run.

    Two things have to be true, and until both are, a saved search is a thing
    that looks scheduled and isn't: automatic runs have to be installed, and they
    have to be working. Installed-but-blocked is the state worth naming — a task
    left pointing at the folder's old location, or a scheduler that stopped
    checking in, both read as "on" everywhere else.

    On anything but macOS and Windows there is no schedule to install, so there's
    nothing to insist on. Saved searches are still worth having there: they run
    by hand with `--run NAME`, which is what the settings window says.
    """
    if os_name() == "other":
        return True
    return schedule_installed() and not schedule_problems()


def schedule_problems():
    """Reasons automatic runs could be installed and still do nothing. Each is
    something the user can act on, phrased for the settings window."""
    if not schedule_installed():
        return []
    if not schedule_points_here():
        # Plain text: the window renders this box with textContent, so any
        # markdown would show up as literal asterisks.
        return ["Automatic runs are pointing at a different location for this "
                "folder, which happens when it gets moved or renamed. Click "
                "Turn off and then Turn on again to fix it."]
    beat = last_check_in()
    if not beat:
        return permission_help()
    when = parse_iso(beat.get("at"))
    if when and now_local() - when > timedelta(seconds=TICK_SECONDS * 3):
        # A sweep can run for hours, and no new tick starts while one is going,
        # so an old heartbeat during a live run is normal, not a problem.
        holder = run_lock._read_holder()
        if holder and _pid_alive(int(holder.get("pid") or 0)):
            return []
        return [f"The scheduler last checked in {fmt_when(when)}, which is longer "
                f"ago than the {TICK_SECONDS // 60} minutes it should be. If this "
                f"computer has been awake since then, turn automatic runs off and "
                f"on again."]
    return []


# ---------- the computer's own power settings ----------
# The Email & Setup tab lists the ones that affect whether a scheduled search
# finds the machine asleep and willing. Anything already set the way we
# recommend is left off the page, so the list isn't a to-do of finished work.
# Reading is unprivileged (`pmset -g`, `powercfg /query`); a failure to read
# leaves the item up rather than claiming a setting we couldn't see.

# Setting cards the window can hide. mac_lid and win_saver stay, because there
# is no preference to read for either.
SYS_MAC_WAKE = "mac_wake"
SYS_MAC_LPM = "mac_lpm"
SYS_WIN_WAKE = "win_wake"
SYS_WIN_LID = "win_lid"

# powercfg GUIDs, so a localized Windows still answers. SUB_SLEEP / RTCWAKE
# aliases are English-only on some builds.
_WIN_SUB_SLEEP = "238c9fa8-0aad-41ed-83f4-97be242c8f20"
_WIN_WAKE_TIMERS = "bd3b718a-0680-4d9d-8ab2-e1d2b4ac806d"
_WIN_SUB_BUTTONS = "4f971e89-eebd-4455-a8de-9e59040e7347"
_WIN_LID_ACTION = "5ca83367-6e45-459f-a27b-476b1d01c936"


def computer_settings():
    """Ids of setting cards that already match and should not be shown."""
    system = os_name()
    try:
        if system == "darwin":
            return _mac_settings_done()
        if system == "windows":
            return _win_settings_done()
    except Exception:
        return []
    return []


def _pmset_sections(text):
    """`pmset -g custom` into {section name: {key: int}}."""
    sections, name = {}, None
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line:
            continue
        if not raw[:1].isspace() and line.endswith(":"):
            name = line[:-1].strip()
            sections[name] = {}
            continue
        if name is None:
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("-").isdigit():
            sections[name][parts[0]] = int(parts[-1])
    return sections


def _pmset_flag(sections, key):
    """(ac, battery) for one pmset key. Missing side is None."""
    ac = (sections.get("AC Power") or {}).get(key)
    battery = (sections.get("Battery Power") or {}).get(key)
    return ac, battery


def _mac_settings_done(text=None):
    """Hide wake-for-network when it's Always, and Low Power Mode when it
    isn't holding runs back on AC (Never, or Only on Battery)."""
    if text is None:
        r = _run(["pmset", "-g", "custom"], timeout=8)
        if r.returncode != 0:
            return []
        text = r.stdout or ""
    sections = _pmset_sections(text)
    if not sections:
        return []
    hide = []
    ac_womp, bat_womp = _pmset_flag(sections, "womp")
    # Always: on both sources, or on AC when there's no battery.
    if ac_womp == 1 and (bat_womp is None or bat_womp == 1):
        hide.append(SYS_MAC_WAKE)
    ac_lpm, _bat_lpm = _pmset_flag(sections, "lowpowermode")
    if ac_lpm == 0:
        hide.append(SYS_MAC_LPM)
    return hide


def _powercfg_indexes(text):
    """Current AC and DC indexes from one powercfg /query block.

    DC is None when the scheme has no battery side (a desktop). Hex or decimal
    both appear; the GUIDs in the same dump are ignored by matching the labels.
    """
    ac = dc = None
    for line in (text or "").splitlines():
        low = line.strip().lower()
        if "current ac power setting index" in low:
            ac = int(low.rsplit(None, 1)[-1], 0)
        elif "current dc power setting index" in low:
            dc = int(low.rsplit(None, 1)[-1], 0)
    return ac, dc


def _win_query(subgroup, setting):
    r = _run(["powercfg", "/query", "SCHEME_CURRENT", subgroup, setting],
             timeout=8)
    if r.returncode != 0:
        return None
    return r.stdout or ""


def _win_settings_done(wake_text=None, lid_text=None):
    """Hide wake timers when both sides are Enable, and the lid when both
    sides are Sleep or Do nothing."""
    hide = []
    if wake_text is None:
        wake_text = _win_query(_WIN_SUB_SLEEP, _WIN_WAKE_TIMERS)
    if wake_text is not None:
        ac, dc = _powercfg_indexes(wake_text)
        # Enable is 1. 0 is Disable, 2 is Important wake timers only.
        if ac == 1 and (dc is None or dc == 1):
            hide.append(SYS_WIN_WAKE)
    if lid_text is None:
        lid_text = _win_query(_WIN_SUB_BUTTONS, _WIN_LID_ACTION)
    if lid_text is not None:
        ac, dc = _powercfg_indexes(lid_text)
        # 0 Do nothing, 1 Sleep, 2 Hibernate, 3 Shut down.
        if ac in (0, 1) and (dc is None or dc in (0, 1)):
            hide.append(SYS_WIN_LID)
    return hide


# ---------- what the settings window calls ----------
# Only these come from the search form. Anything else the form collects
# (debug_dump) is about one interactive run and has no meaning for something
# that runs unattended at 5am.
SAVED_FIELDS = ("queries", "query", "cities", "exact", "min_price", "max_price",
                "min_year", "max_year", "include_no_year", "radius_miles",
                "exclude", "do_descriptions", "do_thumbs", "pace", "limit",
                "interval", "email_to", "name", "enabled")


def searches_for_ui():
    """Scheduled searches with their times already turned into readable phrases, so
    the window doesn't have to reimplement any of the schedule arithmetic."""
    searches = load_searches()
    tracking = {}
    try:
        con = storage.open_db(storage.DB_PATH)
        try:
            for s in searches:
                row = latest_run(con, s.get("id"))
                tracking[s["id"]] = row[3] if row else None
        finally:
            con.close()
    except Exception:
        pass
    out = []
    for s in searches:
        out.append({
            **{k: s.get(k) for k in SAVED_FIELDS},
            "id": s.get("id"),
            "every_text": describe_interval(s.get("interval") or {}),
            "last_text": fmt_when(parse_iso(s.get("last_started"))),
            "next_text": (fmt_when(parse_iso(s.get("next_run")))
                          if s.get("enabled") else "paused"),
            "tracking": tracking.get(s.get("id")),
        })
    return {"searches": out}


def _from_form(payload):
    rec = {k: payload.get(k) for k in SAVED_FIELDS if k in payload}
    iv = rec.get("interval") or {}
    rec["interval"] = {"every": int(iv.get("every") or 1),
                       "unit": iv.get("unit") or "days"}
    # The form sends the boxes it has; the one-line version is ours to derive,
    # never the window's to send.
    if "queries" in rec:
        rec["queries"] = listings.query_list(rec["queries"])[:listings.MAX_QUERIES]
        rec["query"] = listings.query_label(rec["queries"])
    return rec


def ui_hooks():
    """The callbacks settings_ui.py exposes to its page. Each returns a plain
    dict; an "error" key is shown to the user and nothing else happens."""

    def save_search(payload):
        # Two things have to be in place first, and a search saved without either
        # is the same failure: one that looks scheduled, and quietly is not. The
        # window bars this itself, so getting here means it was out of date about
        # one of them — email taken away, or automatic runs turned off, since it
        # opened. Each answer says which, so the block it should have been showing
        # goes up.
        #
        # No email means a search that runs on time, finds things and tells
        # nobody. That's what this used to do, and because the step that was
        # missed is two tabs away, nothing about the silence pointed back at it.
        if not email_ready():
            return {"error": "Set up your email on the Email & Setup tab.",
                    "email_ready": False}
        # And no automatic runs means nothing ever starts it.
        if not scheduling_available():
            return {"error": "Turn automatic runs on first, on the Email & "
                             "Setup tab.",
                    "schedule_ready": False}
        rec = _from_form(payload)
        editing = payload.get("id")
        if editing:
            updated, err = update_search(editing, rec)
            if err:
                return {"error": err}
            what = f"Updated “{updated['name']}”."
        else:
            updated, err = add_search(rec)
            if err:
                return {"error": err}
            what = (f"Saved “{updated['name']}”. It runs "
                    f"{describe_interval(updated['interval'])}, next "
                    f"{fmt_when(parse_iso(updated['next_run']))}.")
        msgs = [what]
        if os_name() == "other":
            # The one system where saving doesn't imply anything will start it.
            msgs.append("There are no automatic runs on this system, so start "
                        "this one yourself with --run when you want it.")
        else:
            # Saving may have changed which times of day the Mac has to wake
            # at, so the queue is brought up to date — the one step here that
            # can put up a password prompt. Skipped when nothing changed.
            _, note = renew_wakes()
            if note:
                msgs.append(note)
        return {"message": " ".join(msgs),
                "warnings": interval_warnings(updated, load_searches()),
                **searches_for_ui()}

    def do_update(search_id, changes):
        _, err = update_search(search_id, changes)
        if err:
            return {"error": err}
        # Pausing or resuming an hourly search changes which times of day the
        # Mac has to wake at. The note rides along for the window to show when
        # the rewrite was refused or worth reporting.
        _, note = renew_wakes()
        return {**searches_for_ui(), **({"note": note} if note else {})}

    def do_delete(search_id):
        _, err = delete_search(search_id)
        if err:
            return {"error": err}
        _, note = renew_wakes()
        return {**searches_for_ui(), **({"note": note} if note else {})}

    def check(payload):
        rec = _from_form(payload)
        rec["id"] = payload.get("id")
        others = [s for s in load_searches() if s.get("id") != payload.get("id")]
        return {"warnings": interval_warnings(rec, others)}

    def save_email(cfg):
        keep = load_email_config()
        merged = {**keep, **{k: v for k, v in cfg.items() if v not in (None, "")}}
        if cfg.get("provider") != "other":
            # Switching back to a known provider must clear a custom server, or
            # the old host would silently keep winning.
            merged["host"] = ""
        # A config written when there was a "send reports to" box still has the
        # address it held. Nothing reads it now, so it goes on the next save
        # rather than sitting in the file looking like a setting.
        merged.pop("default_to", None)
        # Checked before saving, because the alternative is a mistyped address
        # sitting in a file until a run at 5am fails on it — and a bad address
        # fails as "the server rejected that password", which sends you looking
        # in the wrong place entirely.
        problem = address_problem(merged.get("address"), "Your email address")
        if problem:
            return {"error": f"{problem} Nothing was saved."}
        save_email_config(merged)
        # Whether scheduled searches are usable now hangs on this, so every answer
        # says which of the two states it left behind rather than only the
        # happy one.
        if not email_ready(merged):
            return {"error": "Saved, but there's no email address and app "
                             "password yet, so reports can't be sent.",
                    "ready": False}
        note = (f"Saved. Reports will be sent from {merged['address']}, to "
                f"whichever email address each scheduled search asks for. Send "
                f"a test message to be sure it works.")
        return {"message": " ".join([note] + email_remarks(merged)),
                "ready": True}

    def test_email():
        cfg = load_email_config()
        if not email_ready(cfg):
            return {"error": "Add your email address and app password first, "
                             "then Save."}
        to = cfg["address"]
        try:
            send_email(cfg, to, "Faceplace Marketbook: test message",
                       "If you're reading this, scheduled reports will "
                       "reach you.")
        except Exception as e:
            return {"error": f"Couldn't send it: {_smtp_hint(e, cfg)}"}
        note = f"Sent to {to}."
        return {"message": note}

    def state():
        installed = schedule_installed()
        if os_name() == "other":
            hint = ("Automatic runs can only be set up on macOS and Windows. You "
                    "can still run scheduled searches by hand.")
        elif installed:
            nxt = earliest_next_run()
            hint = (f"This computer checks for due searches every "
                    f"{TICK_SECONDS // 60} minutes while awake."
                    + (f" The next one is due {fmt_when(nxt)}." if nxt else
                       " Nothing is scheduled yet."))
            beat = last_check_in()
            if beat and parse_iso(beat.get("at")):
                hint += f" Last checked {fmt_when(parse_iso(beat['at']))}."
        else:
            hint = ("Turn on automatic runs to enable scheduled searches.")
        # `ready` is the one thing the rest of the window acts on, and it is not
        # the same as `installed`: on and blocked runs nothing. Worked out here so
        # the page never has to decide for itself what counts.
        return {"installed": installed, "hint": hint,
                "problems": schedule_problems(),
                "ready": scheduling_available(),
                # Which computer this is, so the window can show the system
                # settings for it and only for it. Instructions for the other
                # one are worse than none: they name menus that aren't there.
                "os": os_name(),
                # What the hourly-interval dropdown offers and where its grid is
                # anchored, so the page never invents its own copy of either.
                "hour_choices": list(HOUR_CHOICES), "daily_hour": DAILY_HOUR,
                "wakes": wake_queue_state(),
                # Setting cards already matching what we recommend, so the
                # window can leave them off. See computer_settings().
                "hide_settings": computer_settings()}

    def set_schedule(on):
        ok, msgs = install_schedule() if on else uninstall_schedule()
        return {"ok": ok, "messages": msgs}

    def do_renew_wakes():
        changed, note = renew_wakes(force=True)
        if not changed:
            return {"error": note or "There was nothing to renew."}
        return {"message": note or "The wake-up schedule is up to date.",
                "wakes": wake_queue_state()}

    def email_for_ui():
        # The window opens knowing whether scheduled searches are usable, so the
        # tabs that offer them are in the right state before anything is
        # clicked rather than after the first refusal.
        cfg = load_email_config()
        return {**cfg, "ready": email_ready(cfg)}

    return {
        "list_searches": searches_for_ui,
        "save_search": save_search,
        "update_search": do_update,
        "delete_search": do_delete,
        "check_schedule": check,
        "email_config": email_for_ui,
        "save_email": save_email,
        "test_email": test_email,
        "schedule_state": state,
        "set_schedule": set_schedule,
        "renew_wakes": do_renew_wakes,
        "units": UNITS if DEV_MODE else ("hours", "days"),
    }


def _self_send(cfg, to):
    """Gmail sometimes files mail you send to yourself under Sent instead of
    delivering it, so that case earns a heads-up. Handles you+tag@gmail.com."""
    if (cfg.get("provider") or "gmail") != "gmail":
        return False
    def norm(a):
        name, _, dom = (a or "").partition("@")
        return f"{name.split('+')[0].replace('.', '')}@{dom}".lower()
    return norm(to) == norm(cfg.get("address"))


def _smtp_hint(e, cfg=None):
    """SMTP errors are unreadable by design. Translate the ones that actually
    happen into something a person can act on."""
    text = str(e)
    if isinstance(e, smtplib.SMTPAuthenticationError) or "5.7.8" in text:
        address = (cfg or {}).get("address") or "your email address"
        return (f"the mail server wouldn't accept {address} with that password. "
                f"For Gmail it has to be a 16-character app password, not your "
                f"normal password — and check the email address itself for a "
                f"typo, because a wrong email address fails the same way.")
    if isinstance(e, (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused)):
        return (f"the mail server rejected one of the email addresses ({text}). "
                f"Check them for a typo.")
    if isinstance(e, (OSError, smtplib.SMTPConnectError)):
        return f"couldn't reach the mail server ({text}). Check your connection."
    return text


# ---------- command line ----------
def cmd_list():
    searches = load_searches()
    if not searches:
        print("No scheduled searches yet. Make one in the settings window.")
        return
    print(f"Automatic runs: {'on' if schedule_installed() else 'off'}\n")
    for s in searches:
        state = "paused" if not s.get("enabled") else describe_interval(s["interval"])
        print(f"{s['name']}  [{state}]")
        print(f"    {searched_for(s)} across {len(s.get('cities') or [])} cities")
        print(f"    last run {fmt_when(parse_iso(s.get('last_started')))}, "
              f"next {fmt_when(parse_iso(s.get('next_run')))}")
        if s.get("email_to"):
            print(f"    reports to {s['email_to']}")


def cmd_test_email():
    cfg = load_email_config()
    if not email_ready(cfg):
        raise SystemExit("Email isn't set up. Add your email address and app "
                         "password in the settings window first.")
    to = cfg["address"]
    send_email(cfg, to, "Faceplace Marketbook: test message",
               "If you're reading this, scheduled reports will reach you.\n\n"
               "Sent by Faceplace Marketbook's email test.")
    print(f"Sent to {to}.")



def cmd_verify_probe(urls):
    """Calibration tool: classify real listing URLs and show the marker that
    decided each one, so the rules above can be checked against reality."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = browser.launch_context(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        browser.ensure_logged_in(page, timeout_s=120)
        for url in urls:
            status, marker = probe_listing(ctx, url)
            tier = "1"
            if status == STATUS_UNKNOWN:
                status, marker = probe_listing_rendered(page, url)
                tier = "2"
            print(f"{status:8} tier{tier}  {marker[:70]:70} {url}")
            browser.human_pause(1.5, 3.0)
        ctx.close()


def main():
    ap = argparse.ArgumentParser(
        description="Searches that run themselves on a schedule and email you "
                    "the results.")
    ap.add_argument("--tick", action="store_true",
                    help="run every scheduled search that is due (what the OS calls)")
    ap.add_argument("--run", metavar="NAME",
                    help="run one scheduled search now, ignoring its schedule")
    ap.add_argument("--list", action="store_true", dest="do_list",
                    help="show scheduled searches and when they run next")
    ap.add_argument("--install", action="store_true",
                    help="let the OS wake this computer and run searches")
    ap.add_argument("--uninstall", action="store_true", help="turn automatic runs off")
    ap.add_argument("--test-email", action="store_true",
                    help="send yourself a test message")
    ap.add_argument("--no-email", action="store_true",
                    help="run but don't send the report")
    ap.add_argument("--verify-probe", nargs="+", metavar="URL",
                    help="classify listing URLs as live, sold or gone")
    ap.add_argument("--serve-galleries", action="store_true",
                    help="answer localhost links to this computer's galleries")
    a = ap.parse_args()

    if a.do_list:
        cmd_list()
    elif a.test_email:
        cmd_test_email()
    elif a.verify_probe:
        cmd_verify_probe(a.verify_probe)
    elif a.serve_galleries:
        serve_galleries()
    elif a.install:
        ok, msgs = install_schedule()
        for m in msgs:
            print(("" if ok else "!! ") + m)
        raise SystemExit(0 if ok else 1)
    elif a.uninstall:
        ok, msgs = uninstall_schedule()
        for m in msgs:
            print(m)
    elif a.run:
        tick(force=a.run, send=not a.no_email)
    elif a.tick:
        # First, before anything that could fail: the check-in is what proves the
        # OS can reach this code at all.
        check_in("tick")
        results = tick(send=not a.no_email)
        if not results:
            log("Nothing was due.")
        check_in("finished", ran=len(results))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
