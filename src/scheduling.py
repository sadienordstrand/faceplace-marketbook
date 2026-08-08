#!/usr/bin/env python3
"""
scheduling.py
-------------
Saved searches that run themselves on a schedule and email you the results.

    python3 src/scheduling.py --tick          # run whatever is due (what the OS calls)
    python3 src/scheduling.py --list          # show saved searches and when they run
    python3 src/scheduling.py --run NAME      # run one now, ignoring its schedule
    python3 src/scheduling.py --install       # let the OS wake the machine and run ticks
    python3 src/scheduling.py --uninstall
    python3 src/scheduling.py --test-email
    python3 src/scheduling.py --verify-probe URL

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
import ssl
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

import browser
import descriptions
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

# A run this far past its scheduled time gets reported as late, which is how a
# laptop that was asleep or switched off shows up in your inbox.
LATE_AFTER_HOURS = 2

# Advisory thresholds. Frequent automated sweeps are what gets Marketplace
# accounts limited, so the UI warns past these but never refuses.
SAFE_MIN_INTERVAL_HOURS = 6
SAFE_MAX_SEARCHES = 3

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
        # 11:12pm into a report as "started tomorrow at 5:12 am".
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.replace(microsecond=0)


def _hour12(dt):
    # %-I is not portable to Windows, so format the hour by hand.
    h = dt.hour % 12 or 12
    return f"{h}:{dt:%M} {'am' if dt.hour < 12 else 'pm'}"


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


# ---------- saved searches ----------
DEFAULT_SEARCH = {
    "enabled": True,
    "query": "",
    "cities": [],
    "exact": False,
    "min_price": None,
    "max_price": None,
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


def load_searches():
    if not SEARCHES_PATH.exists():
        return []
    try:
        data = json.loads(SEARCHES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        # Never silently start from scratch: that would quietly delete every
        # saved search the moment the file got a stray character in it.
        raise SystemExit(f"{SEARCHES_PATH.name} is unreadable ({e}). Fix or move "
                         f"the file; it has not been touched.")
    searches = data.get("searches", []) if isinstance(data, dict) else data
    return [{**DEFAULT_SEARCH, **s} for s in searches]


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


def validate_search(search, searches, editing_id=None):
    name = (search.get("name") or "").strip()
    if not name:
        return "Give the search a name."
    if not (search.get("query") or "").strip():
        return "A saved search needs something to search for."
    if not search.get("cities"):
        return "Pick at least one city."
    for s in searches:
        if s.get("id") != editing_id and (s.get("name") or "").lower() == name.lower():
            return f"You already have a saved search called '{name}'."
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
    """Advisory only. Frequent sweeps and lots of saved searches are the two
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
            f"That would make {enabled + 1} active saved searches. Each one is a "
            f"full sweep of every city you picked, so several of them multiply the "
            f"traffic even on long intervals. Consider keeping fewer, or spacing "
            f"them further apart.")
    return out


def add_search(search):
    searches = load_searches()
    # Fill the defaults in before validating, so a caller that only supplies a
    # name, query and cities isn't told its interval is invalid.
    rec = {**DEFAULT_SEARCH, **{k: v for k, v in search.items() if v is not None}}
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
        return None, f"No saved search with id '{search_id}'."
    merged = {**rec, **changes}
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
        return None, f"No saved search with id '{search_id}'."
    save_searches([s for s in searches if s.get("id") != rec.get("id")])
    return rec, None


# ---------- when does it run next ----------
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

    step = timedelta(minutes=n) if unit == "minutes" else timedelta(hours=n)
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
DEFAULT_EMAIL_CONFIG = {
    "provider": "gmail",
    "host": "",
    "port": 587,
    "address": "",
    "app_password": "",
    "default_to": "",
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

# Providers that expect the address to end this way. Google Workspace serves
# custom domains from smtp.gmail.com, so a mismatch is worth a remark and not a
# refusal.
PROVIDER_DOMAINS = {
    "gmail": ("gmail.com", "googlemail.com"),
    "outlook": ("outlook.com", "hotmail.com", "live.com", "msn.com"),
    "icloud": ("icloud.com", "me.com", "mac.com"),
}


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
    address = (cfg.get("address") or "").strip().lower()
    provider = cfg.get("provider") or "gmail"
    domains = PROVIDER_DOMAINS.get(provider)
    if address and domains and not address.endswith(tuple(f"@{d}" for d in domains)):
        out.append(f"You picked {provider.title()} but {address} isn't "
                   f"{' or '.join(domains)}. That's right for a work or school "
                   f"account on {provider.title()}, and wrong otherwise — the "
                   f"test send will tell you which.")
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
        raise RuntimeError("Email isn't set up yet — add your address and app "
                           "password in the settings window.")
    to = (to or cfg.get("default_to") or cfg["address"]).strip()
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
    name = search.get("name") or search.get("query") or "Saved search"

    subject = f"{name}: {len(new_rows)} new, {total} total"
    if removed:
        subject += f", {len(removed)} gone"

    T = []
    for w in warnings:
        T.append(f"!! {w}")
    if warnings:
        T.append("")
    T.append(name)
    T.append(f"Searched for '{search.get('query')}' across "
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

    T.append("The full gallery, with every thumbnail, is on your computer at:")
    T.append(f"  {summary.get('gallery') or summary.get('run_dir')}")
    T.append("The attached files are stripped-down copies so they fit in an email.")
    T.append("")
    if next_run:
        T.append(f"Next run: {fmt_when(next_run)}.")
    T.append("To pause or change this search, open Faceplace Marketbook and go to "
             "the Saved searches tab.")

    html = _report_html(name, search, summary, new_rows, sold, gone, total, dur,
                        started, next_run, warnings)
    return subject, "\n".join(T), html


def _esc(s):
    return (str(s if s is not None else "").replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


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
<p style="color:#6b6b5e;margin:0 0 20px">&lsquo;{_esc(search.get('query'))}&rsquo;
across {_plural(len(search.get('cities') or []), 'city', 'cities')} &middot; started
{_esc(fmt_when(started))} &middot; took {_esc(dur)}</p>
<table style="border-collapse:collapse;margin:0 0 8px"><tr>{stat_html}</tr></table>
{sections}
<hr style="border:0;border-top:1px solid #e4e2d6;margin:26px 0 16px">
<p style="color:#6b6b5e;font-size:13px;margin:0 0 8px">The full gallery, with every
thumbnail, is on your computer at<br>
<code style="color:{ink};word-break:break-all">{_esc(summary.get('gallery') or summary.get('run_dir'))}</code><br>
The attached files are stripped-down copies so they fit in an email.</p>
{next_html}
<p style="color:#6b6b5e;font-size:13px;margin:0">To pause or change this search,
open Faceplace Marketbook and go to the <b>Saved searches</b> tab.</p>
</div></body></html>"""


# ---------- attachments ----------
ATTACH_MAX_MB = 12
# Base64 inflates an attachment by about a third and Gmail rejects messages over
# 25 MB, so two files at the per-file limit would not fit together.
COMBINED_MAX_MB = 22


def _encoded_size(n_bytes):
    return int(n_bytes * 4 / 3)


def build_attachments(csv_path, new_ids, out_dir=None, per_file_mb=ATTACH_MAX_MB,
                      combined_mb=COMBINED_MAX_MB):
    """Two stripped-down galleries: just this run's new listings, and everything
    currently tracked. Thumbnails stay in only while the file still fits."""
    import build_gallery
    csv_path = Path(csv_path)
    out_dir = Path(out_dir) if out_dir else csv_path.parent
    limit = per_file_mb * 1024 * 1024
    built = []
    for name, ids in (("new-listings.html", set(new_ids)),
                      ("all-results.html", None)):
        if ids is not None and not ids:
            continue
        path = out_dir / name
        build_gallery.build(csv_path, path, embed=True, budget_mb=per_file_mb,
                            only_ids=ids, quiet=True)
        embedded = True
        if path.stat().st_size > limit:
            build_gallery.build(csv_path, path, embed=False, only_ids=ids,
                                quiet=True)
            embedded = False
        built.append({"name": name, "path": path, "ids": ids,
                      "embedded": embedded, "size": path.stat().st_size})

    # If the pair still won't fit in one message, the full list gives up its
    # thumbnails first: the complete gallery is already on disk, and the new
    # listings are the part actually worth looking at on a phone.
    def combined():
        return sum(_encoded_size(b["size"]) for b in built)

    for b in sorted(built, key=lambda b: b["name"] != "all-results.html"):
        if combined() <= combined_mb * 1024 * 1024:
            break
        if not b["embedded"]:
            continue
        build_gallery.build(csv_path, b["path"], embed=False, only_ids=b["ids"],
                            quiet=True)
        b["embedded"], b["size"] = False, b["path"].stat().st_size

    return [(b["name"], b["path"].read_bytes(), "html") for b in built], built


# ---------- failure notices ----------
REAUTH_STEPS = """What to do:

  1. Open the Faceplace Marketbook folder on your computer and double-click
     "Log into Facebook" — the .command file on a Mac, the .bat file on
     Windows. It opens a browser window and searches nothing.
  2. Log into Facebook by hand, including any two-factor code. Wait until you
     can see your normal Facebook feed.
  3. Check that the Marketplace search radius is still the distance you want.
  4. That's it. The window closes itself and the next scheduled run will work.

Logging into Facebook in Safari, Chrome or Edge will not fix this: the app keeps
its own separate browser login, and that's the one that expired.

Scheduled runs can't log in for you, because Facebook asks for a password and
sometimes a code from your phone. Until you do the steps above, scheduled runs
will keep stopping at this point."""


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
                 "Saved searches tab."]
    text = "\n".join(body)
    try:
        send_email(cfg, to, subject, text)
        log(f"  emailed the {kind.replace('_', ' ')} notice")
    except Exception as e:
        log(f"  couldn't send the {kind} email ({e})")


# ---------- running one saved search ----------
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
    to = (search.get("email_to") or "").strip() or email_cfg.get("default_to")
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

        log(f"Running '{search['name']}' ({search.get('query')!r}, "
            f"{len(search.get('cities') or [])} cities)"
            + (f" — {late:.1f}h late" if late >= LATE_AFTER_HOURS else ""))

        runner = sweep or fb.run
        summary = runner(
            search.get("query"), fb.DEFAULT_SCROLLS, bool(search.get("exact")),
            thumbs_dir=fb.THUMBS_DIRNAME,
            do_descriptions=bool(search.get("do_descriptions", True)),
            do_thumbs=bool(search.get("do_thumbs", True)),
            do_gallery=True, pace=search.get("pace") or "fast",
            exclude=[t.strip() for t in (search.get("exclude") or "").split(",")
                     if t.strip()],
            min_price=search.get("min_price"), max_price=search.get("max_price"),
            limit=search.get("limit"), assume_yes=True,
            only_labels=search.get("cities"), open_gallery=False, no_pause=True,
            run_dir=run_dir, previous_rows=prev_rows, describe_new_only=True,
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
        if km and km < fb.EXPECTED_RADIUS_KM:
            warnings.append(
                f"Your Marketplace search radius is set to about "
                f"{round(km / 1.609)} miles, rather than the 500 Facebook "
                f"allows, so this run only looked that far out from each city. "
                f"If you wanted a wider net, open Faceplace Marketbook and "
                f"raise the radius.")
        if summary.get("interrupted"):
            warnings.append("The run was interrupted partway through, so some "
                            "descriptions and photos are missing.")
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
                log(f"  {b['name']}: {b['size'] / 1e6:.1f} MB"
                    f"{'' if b['embedded'] else ', thumbnails dropped to fit'}")
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
    from fb_marketplace_sweep import SessionExpired

    now = now or now_local()
    searches = load_searches()
    if force:
        rec = find_search(searches, force)
        if not rec:
            raise SystemExit(f"No saved search called '{force}'.")
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
                to = (search.get("email_to") or "").strip() or email_cfg.get("default_to")
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
    rearm_wake()
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


def rearm_wake(quiet=True):
    """Try to give the next due run its own one-off scheduled wake.

    `pmset schedule` needs root, and this runs unattended (from a tick or a
    button press), where nothing can show a password prompt — so `sudo -n`
    only succeeds on a machine set up to allow pmset without a password.
    Everywhere else this quietly does nothing, and the daily wake that
    install_schedule sets (with a real password prompt) is what wakes the Mac.
    Searches on hour intervals then run when the machine is next awake."""
    if os_name() != "darwin":
        return False
    nxt = earliest_next_run()
    if not nxt:
        return False
    # A little early, so we're already awake when the tick fires.
    when = nxt - timedelta(minutes=2)
    if when <= now_local():
        return False
    stamp = when.strftime("%m/%d/%y %H:%M:%S")
    r = _run(["sudo", "-n", "pmset", "schedule", "wake", stamp])
    if r.returncode != 0:
        if not quiet:
            log(f"  couldn't schedule a wake ({r.stderr.strip() or 'permission denied'})")
        return False
    if not quiet:
        log(f"  Mac will wake at {fmt_when(when)} for the next run")
    return True


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
            f"folder, and that's where this one lives. Automatic runs are set up "
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


def _pmset_as_admin(args):
    """Run a pmset command with root rights, prompting if needed.

    pmset refuses everything but reads without root. `sudo -n` succeeds
    instantly when credentials are already cached; otherwise osascript puts up
    the standard macOS administrator-password dialog — the prompt the README
    tells people to expect. Returns True on success."""
    if _run(["sudo", "-n", "pmset"] + args).returncode == 0:
        return True
    cmd = " ".join(["pmset"] + args)
    r = _run(["osascript", "-e",
              f'do shell script "{cmd}" with administrator privileges'])
    return r.returncode == 0


def set_daily_wake():
    """Set the repeating {DAILY_HOUR}am wake, and say what happened.

    wakeorpoweron can even start a Mac that was shut down, as long as someone
    unlocks it afterwards when FileVault is on."""
    args = ["repeat", "wakeorpoweron", "MTWRFSU", f"{DAILY_HOUR:02d}:00:00"]
    if _pmset_as_admin(args):
        return (f"Your Mac will wake itself at {DAILY_HOUR}am for daily "
                f"searches. Searches on hour intervals run whenever the Mac "
                f"is next awake.")
    return (f"Couldn't set the daily wake — the password prompt was cancelled "
            f"or failed. Runs still happen, but only once the Mac is awake. "
            f"To set the wake yourself, open Terminal and run:\n"
            f"    sudo pmset repeat wakeorpoweron MTWRFSU "
            f"{DAILY_HOUR:02d}:00:00")


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
                    f"{TICK_SECONDS // 60} minutes.")
        if daily_wake:
            msgs.append(set_daily_wake())
        rearm_wake(quiet=False)
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
        msgs.append("One setting Windows won't let us change for you: open "
                    "Control Panel > Power Options > Change plan settings > "
                    "Change advanced power settings > Sleep > Allow wake timers, "
                    "and set it to Enable for both On battery and Plugged in. On "
                    "battery it defaults to blocking wake timers, which would "
                    "stop scheduled runs.")
        return True, msgs

    return False, ["Automatic runs are only set up for macOS and Windows."]


def win_task_xml():
    exe = python_exe()
    start = (now_local() + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%S")
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Runs Faceplace Marketbook saved searches when they are due.</Description>
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
        msgs.append("Automatic runs are off. Your saved searches are untouched — "
                    "you can still run them by hand.")
        if not _pmset_as_admin(["repeat", "cancel"]):
            msgs.append("The daily 5am wake couldn't be removed without your "
                        "password. It's harmless on its own; to remove it, open "
                        "Terminal and run:\n    sudo pmset repeat cancel")
        return True, msgs
    if system == "windows":
        _run(["schtasks", "/delete", "/tn", WIN_TASK, "/f"])
        msgs.append("Automatic runs are off. Your saved searches are untouched.")
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


def schedule_problems():
    """Reasons automatic runs could be installed and still do nothing. Each is
    something the user can act on, phrased for the settings window."""
    if not schedule_installed():
        return []
    if not schedule_points_here():
        return ["Automatic runs are pointing at a different location for this "
                "folder, which happens when it gets moved or renamed. Turn them "
                "off and on again to fix it."]
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


# ---------- what the settings window calls ----------
# Only these come from the search form. Anything else the form collects
# (debug_dump, do_gallery, the budget prompt) is about one interactive run and
# has no meaning for something that runs unattended at 5am.
SAVED_FIELDS = ("query", "cities", "exact", "min_price", "max_price", "exclude",
                "do_descriptions", "do_thumbs", "pace", "limit", "interval",
                "email_to", "name", "enabled")


def searches_for_ui():
    """Saved searches with their times already turned into readable phrases, so
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
    return rec


def ui_hooks():
    """The callbacks settings_ui.py exposes to its page. Each returns a plain
    dict; an "error" key is shown to the user and nothing else happens."""

    def save_search(payload):
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
        if not schedule_installed():
            msgs.append("Automatic runs are still off — turn them on from the "
                        "Email & schedule tab, or this won't run on its own.")
        elif os_name() == "darwin":
            rearm_wake()
        return {"message": " ".join(msgs),
                "warnings": interval_warnings(updated, load_searches()),
                **searches_for_ui()}

    def do_update(search_id, changes):
        _, err = update_search(search_id, changes)
        if err:
            return {"error": err}
        return searches_for_ui()

    def do_delete(search_id):
        _, err = delete_search(search_id)
        if err:
            return {"error": err}
        return searches_for_ui()

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
        if not merged.get("default_to"):
            merged["default_to"] = merged.get("address") or ""
        # Checked before saving, because the alternative is a mistyped address
        # sitting in a file until a run at 5am fails on it — and a bad address
        # fails as "the server rejected that password", which sends you looking
        # in the wrong place entirely.
        for value, what in ((merged.get("address"), "Your email address"),
                            (merged.get("default_to"), "The address to send to")):
            problem = address_problem(value, what)
            if problem:
                return {"error": f"{problem} Nothing was saved."}
        save_email_config(merged)
        if not email_ready(merged):
            return {"error": "Saved, but there's no address and app password yet, "
                             "so reports can't be sent."}
        note = (f"Saved. Reports will come from {merged['address']} to "
                f"{merged['default_to']}. Send a test message to be sure it "
                f"works.")
        return {"message": " ".join([note] + email_remarks(merged))}

    def test_email():
        cfg = load_email_config()
        if not email_ready(cfg):
            return {"error": "Add your address and app password first, then Save."}
        to = cfg.get("default_to") or cfg["address"]
        try:
            send_email(cfg, to, "Faceplace Marketbook: test message",
                       "If you're reading this, scheduled reports will "
                       "reach you.")
        except Exception as e:
            return {"error": f"Couldn't send it: {_smtp_hint(e, cfg)}"}
        note = f"Sent to {to}."
        if _self_send(cfg, to):
            note += (" One Gmail quirk: mail you send to yourself is sometimes "
                     "filed under Sent Mail instead of the inbox, so look there "
                     "too. Sending reports to a different address avoids it.")
        return {"message": note}

    def state():
        installed = schedule_installed()
        if os_name() == "other":
            hint = ("Automatic runs can only be set up on macOS and Windows. You "
                    "can still run saved searches by hand.")
        elif installed:
            nxt = earliest_next_run()
            hint = (f"This computer checks for due searches every "
                    f"{TICK_SECONDS // 60} minutes."
                    + (f" The next one is due {fmt_when(nxt)}." if nxt else
                       " Nothing is scheduled yet."))
            beat = last_check_in()
            if beat and parse_iso(beat.get("at")):
                hint += f" Last checked {fmt_when(parse_iso(beat['at']))}."
        else:
            hint = ("Nothing runs on its own yet. Turning this on lets your "
                    "computer wake itself up at the scheduled time, run the "
                    "search, and email you the results.")
            if os_name() == "darwin":
                hint += (" macOS will ask for your password, because waking a "
                         "sleeping Mac on a schedule needs administrator rights.")
        return {"installed": installed, "hint": hint,
                "problems": schedule_problems()}

    def set_schedule(on):
        ok, msgs = install_schedule() if on else uninstall_schedule()
        return {"ok": ok, "messages": msgs}

    return {
        "list_searches": searches_for_ui,
        "save_search": save_search,
        "update_search": do_update,
        "delete_search": do_delete,
        "check_schedule": check,
        "email_config": load_email_config,
        "save_email": save_email,
        "test_email": test_email,
        "schedule_state": state,
        "set_schedule": set_schedule,
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
        address = (cfg or {}).get("address") or "your address"
        return (f"the mail server wouldn't accept {address} with that password. "
                f"For Gmail it has to be a 16-character app password, not your "
                f"normal password — and check the address itself for a typo, "
                f"because a wrong address fails the same way.")
    if isinstance(e, (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused)):
        return (f"the mail server rejected one of the addresses ({text}). Check "
                f"them for a typo.")
    if isinstance(e, (OSError, smtplib.SMTPConnectError)):
        return f"couldn't reach the mail server ({text}). Check your connection."
    return text


# ---------- command line ----------
def cmd_list():
    searches = load_searches()
    if not searches:
        print("No saved searches yet. Make one in the settings window.")
        return
    print(f"Automatic runs: {'on' if schedule_installed() else 'off'}\n")
    for s in searches:
        state = "paused" if not s.get("enabled") else describe_interval(s["interval"])
        print(f"{s['name']}  [{state}]")
        print(f"    {s.get('query')!r} across {len(s.get('cities') or [])} cities")
        print(f"    last run {fmt_when(parse_iso(s.get('last_started')))}, "
              f"next {fmt_when(parse_iso(s.get('next_run')))}")
        if s.get("email_to"):
            print(f"    reports to {s['email_to']}")


def cmd_test_email():
    cfg = load_email_config()
    if not email_ready(cfg):
        raise SystemExit("Email isn't set up. Add your address and app password "
                         "in the settings window first.")
    to = cfg.get("default_to") or cfg["address"]
    send_email(cfg, to, "Faceplace Marketbook: test message",
               "If you're reading this, scheduled reports will reach you.\n\n"
               "Sent by Faceplace Marketbook's email test.")
    print(f"Sent to {to}.")
    if _self_send(cfg, to):
        print("Because you're sending to the same Gmail account you're sending\n"
              "from, the copy may be filed under Sent Mail instead of your\n"
              "inbox, so look there too. Sending reports to a different address\n"
              "avoids that.")


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
        description="Saved searches that run themselves and email you the results.")
    ap.add_argument("--tick", action="store_true",
                    help="run every saved search that is due (what the OS calls)")
    ap.add_argument("--run", metavar="NAME",
                    help="run one saved search now, ignoring its schedule")
    ap.add_argument("--list", action="store_true", dest="do_list",
                    help="show saved searches and when they run next")
    ap.add_argument("--install", action="store_true",
                    help="let the OS wake this computer and run searches")
    ap.add_argument("--uninstall", action="store_true", help="turn automatic runs off")
    ap.add_argument("--test-email", action="store_true",
                    help="send yourself a test message")
    ap.add_argument("--no-email", action="store_true",
                    help="run but don't send the report")
    ap.add_argument("--verify-probe", nargs="+", metavar="URL",
                    help="classify listing URLs as live, sold or gone")
    a = ap.parse_args()

    if a.do_list:
        cmd_list()
    elif a.test_email:
        cmd_test_email()
    elif a.verify_probe:
        cmd_verify_probe(a.verify_probe)
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
