#!/usr/bin/env python3
"""
fb_marketplace_sweep.py
------------------------
Personal-use, low-frequency Facebook Marketplace tooling, and the entry point
the launchers run.

This module is the sweep itself — building a city's search URL, reading the
account's search radius, scrolling a city and knowing when to stop — plus the
pipeline in `run()` that drives every stage in one browser session, and the
command line.

The rest lives next door:

  browser.py       Chromium, the saved login, and the pacing of a run
  locations.py     the cities to search
  listings.py      page -> rows, and which rows survive the filters
  storage.py       the CSV, the database, and the run folders
  descriptions.py  detail pages, and the photos saved from them

README.md is the end-user manual; docs/how-it-works.md covers the internals,
the command-line flags, and the ToS caveats.
"""
import argparse
import csv
import json
import re
import sys
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus, urlsplit

from playwright.sync_api import sync_playwright

import locations
import paths
import storage
import updater
from browser import (SessionExpired, WindowClosed, ensure_logged_in, fmt_dur,
                     goto_with_retry, human_pause, keep_awake, launch_context,
                     stop_if_window_closed)
from descriptions import (DEFAULT_PACE, PACES, PAGE_WORK_SECONDS,
                          PHOTO_SAVE_SECONDS, fetch_thumbs,
                          retrieve_descriptions)
from listings import (EARLIEST_YEAR, ITEM_RE, MAX_QUERIES, SCRIPT_JSON_JS,
                      better_row, build_rows, card_may_keep,
                      extract_json_listings, keep_row, latest_year,
                      query_groups, query_label, query_list, query_numbers,
                      relevance)

# Where downloaded photos go inside a run folder.
THUMBS_DIRNAME = "thumbnails"

# Scroll ceiling per city — a backstop, not the usual stop condition. Facebook
# orders results by relevance, so depth buys steadily weaker matches, and the
# sweep normally quits once matches dry up (see KEEPER_PATIENCE). Not exposed in
# the settings window — change it here if you ever need to reach deeper.
DEFAULT_SCROLLS = 60

# Consecutive scrolls that turn up no listing capable of passing the filters
# before we stop. This, not the ceiling, is what normally ends a city: the
# related-inventory tail keeps producing cards forever, just not relevant ones.
KEEPER_PATIENCE = 3

# The stop_reason a city carries when Ctrl-C ended it rather than one of the
# ordinary conditions. A run is hours long, so stopping it has to be a normal
# way for a stage to end, not an error: whatever has been gathered is kept and
# the run winds up early.
STOPPED_BY_HAND = "interrupted"

# Which stage a stopped run got to. The keys are what run.json and the emailed
# report read; the phrases are for saying it out loud on the way past.
STAGE_NAMES = {
    "sweep": "the sweep",
    "verifying": "the check on listings that had stopped appearing",
    "descriptions": "description retrieval",
    "thumbnails": "the photo downloads",
}

# One evaluate() call per pass: snapshot every result card plus whether it sits
# after the "Results from outside your search" divider.
#
# Finding that divider is worth some care: when we miss it, every card gets
# labelled "unknown", which passes the section filter, so the whole
# out-of-radius tail survives. Two rules keep it accurate without requiring a
# single text node: candidates must be short (a feed-sized container matches the
# phrase too, but runs to thousands of characters), and the deepest match wins,
# discarding any candidate that contains another. That tolerates Facebook
# splitting the phrase across nested spans.
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
    keeps its segment in the URL, which makes the redirect the signal to watch.

    Only the path is checked, not the whole URL: the query string carries the
    search terms, and a segment that happens to appear in them (searching
    'sacramento' with the shipped seg 'sac') must not mask the redirect."""
    try:
        path = urlsplit(page.url or "").path.lower()
    except Exception:
        return False
    return f"/marketplace/{seg.lower()}/" not in path + "/"


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
        warn = ("  <- under Facebook's 500 mi maximum, so each city "
                "searches less ground")
    return f"~{miles} mi ({km} km){warn}"


def show_gallery(path):
    """The finished gallery, in the everyday browser. It's self-contained — the
    photos are baked in — so the file URL is all the browser needs."""
    print(f"Opening {path}")
    try:
        webbrowser.open(Path(path).resolve().as_uri())
    except Exception as e:
        print(f"  couldn't open a browser ({e}); open the file yourself.")


def discard_empty_run_dir(run_dir):
    """Take back the folder a run made for itself, if it never wrote anything.

    The folder is created before the browser opens, so quitting at the login
    screen would otherwise leave an empty one behind — and the Past searches
    tab lists folders, so it would sit there looking like a run that found
    nothing. rmdir refuses to touch a folder with anything in it, which is
    exactly the guard wanted here."""
    if not run_dir:
        return
    try:
        Path(run_dir).rmdir()
    except OSError:
        pass


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
        print(f"  2. Check the search radius. It's set to "
              f"{describe_radius(km, note=False)}. ")
        print("     Facebook allows up to 500 miles; raise it in")
        print("     the location control in the left sidebar for a wider net.")
    elif km:
        print(f"  2. Check the search radius. It reads "
              f"{describe_radius(km, note=False)},")
        print("     the widest Facebook allows.")
    else:
        print("  2. Check the search radius in the left sidebar. Facebook")
        print("     starts you on 250 miles and allows up to 500.")
    print("     The radius is an account setting, so you only set it once.")
    print("\n  3. Come back here and press Enter.")
    print("=" * 66)
    try:
        input("\nPress Enter to start the sweep (Ctrl-C to quit)... ")
    except EOFError:
        print("(no terminal to wait on — starting)")
        return km
    # Closing the browser is the other way to answer this prompt, and there's no
    # sweep to start without it. Nothing has been gathered yet either, so this is
    # the one stage where stopping is simply quitting.
    if page.is_closed():
        raise WindowClosed("The Facebook window was closed before the sweep "
                           "started.")

    after = read_radius_km(page)
    if after and after != km:
        print(f"Radius is now {describe_radius(after, note=False)}.")
    return after or km


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
    stats dict describing where and why scrolling stopped. Ctrl-C is one of the
    ways it can stop: the cards already read off the page are as real as the
    ones a full scroll would have found, so they come back the same way, marked
    with STOPPED_BY_HAND so the caller knows to wind the run up."""
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
                stop_if_window_closed(e)
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
    try:
        for n in range(1, max_scrolls + 1):
            scroll_no = n
            lap = time.time()
            try:
                page.mouse.wheel(0, 5000)
            except Exception as e:
                # Scrolling is the call a closed window interrupts, since it's
                # where a city spends nearly all of its time.
                stop_if_window_closed(e)
                raise
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
    except KeyboardInterrupt:
        print(f"\n  Stopped scrolling. Keeping the {len(cards)} card"
              f"{'' if len(cards) == 1 else 's'} already read off this page.")
        return cards, divider_seen, divider_text, stats(STOPPED_BY_HAND)
    print(f"  hit the {max_scrolls}-scroll ceiling")
    return cards, divider_seen, divider_text, stats("scroll ceiling")


def city_summary(per_query, kept, dropped):
    """One city's numbers, however many queries it took to get them.

    A city is swept once per query, so what used to be a single set of scroll
    counters is now a set per query. They're added up here so that everything
    downstream — the closing summary, run.json, the emailed report — keeps
    reading one line per city, with the individual queries kept alongside for
    when there was more than one to tell apart."""
    stats = list(per_query.values())
    return {
        "queries_run": len(stats),
        "scrolls_used": sum(s["scrolls_used"] for s in stats),
        "scroll_ceiling": sum(s["scroll_ceiling"] for s in stats),
        "cards": sum(s["cards"] for s in stats),
        "keepers_seen": sum(s["keepers_seen"] for s in stats),
        # dict.fromkeys: the reasons in the order they happened, without
        # repeating "no new matches" once per query.
        "stop_reason": ", ".join(dict.fromkeys(s["stop_reason"] for s in stats)),
        "scroll_seconds": round(sum(s["scroll_seconds"] for s in stats), 1),
        "seconds_saved_estimate": round(
            sum(s["seconds_saved_estimate"] for s in stats), 1),
        "divider_seen": any(s["divider_seen"] for s in stats),
        "kept": kept, "dropped": dropped,
        **({"per_query": per_query} if len(stats) > 1 else {}),
    }


def run(query, scrolls, exact, out_csv=None, only=None, keep_all=False,
        debug_dump=False, match=None, limit=None, thumbs_dir=THUMBS_DIRNAME,
        do_descriptions=True, do_thumbs=True, do_gallery=True, pace=DEFAULT_PACE,
        exclude=(), min_price=None, max_price=None, min_year=None, max_year=None,
        include_no_year=True,
        only_labels=None, open_gallery=True, no_pause=False,
        run_dir=None, previous_rows=None, describe_new_only=False, verifier=None,
        login_wait=None, unattended=False):
    """One pass over everything: sweep every saved city, visit each kept
    listing's detail page at most once for its description and full-size photo,
    save that photo locally while its URL is still fresh, then build the
    gallery — all in a single browser session.

    `query` is one query string or a list of up to MAX_QUERIES of them. Facebook
    takes one query per search, so several means several sweeps of every city,
    merged by listing id — a listing is kept if it matches all the words of any
    one of the queries.

    Output goes to its own runs/<query>_<date>/ folder unless out_csv or run_dir
    overrides it. The SQLite database is the cumulative index across every run
    and lives outside the run folders.

    Ctrl-C is a way of finishing, not a way of failing. At any stage it stops
    the work, keeps everything gathered up to that moment, and goes straight to
    the CSV and the gallery — so an hour of sweeping is never thrown away for
    wanting the second hour back. Closing the browser window by hand ends the
    run the same way, for the same reason: see browser.WindowClosed.

    A scheduled search passes the extra arguments: run_dir to write into
    the same folder every time, previous_rows for what the last run found,
    describe_new_only so old listings aren't re-fetched, and verifier to check
    whether the listings that stopped appearing are actually gone. Returns a
    summary dict; interactive callers ignore it."""
    all_locs = locations.load_locations()
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
    queries = query_list(query)[:MAX_QUERIES]
    if not queries:
        print("Nothing to search for.")
        return {"status": "error", "error": "No query to search for."}
    label_all = query_label(queries)
    groups = query_groups(queries)
    numbers = query_numbers(queries)
    if run_dir:
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        out_path = run_dir / "results.csv"
    elif out_csv:
        out_path, run_dir = Path(out_csv), None
    else:
        # Named for the first query alone. Several of them spelled out makes a
        # folder name too long for the systems that have to hold it and too long
        # for anyone to read; the whole search is in run.json either way.
        run_dir = storage.make_run_dir(queries[0])
        out_path = run_dir / "results.csv"
    con = storage.open_db(storage.DB_PATH)
    debug_root = (run_dir / "debug") if run_dir else paths.DEBUG_DIR
    if debug_dump:
        debug_root.mkdir(parents=True, exist_ok=True)
    stages = ["sweep"] + (["retrieve descriptions"] if do_descriptions else []) \
        + (["thumbnails"] if do_thumbs else []) + (["gallery"] if do_gallery else [])
    asked_for = " OR ".join(f"'{q}'" for q in queries)
    print(f"Plan: {' -> '.join(stages)} for {len(locs)} "
          f"location{'s' if len(locs) != 1 else ''}, "
          f"{'queries' if len(queries) > 1 else 'query'} {asked_for}"
          + (f", '{pace}' description pacing." if do_descriptions else "."))
    if len(queries) > 1:
        print(f"  Each city is searched once per query, and a listing is kept if "
              f"it matches every word of any one of them.")
    if run_dir:
        print(f"Output folder: {run_dir}")
    if exclude:
        print(f"Excluding: {', '.join(exclude)}")
    if min_price is not None or max_price is not None:
        print(f"Price filter: {min_price if min_price is not None else 'any'} - "
              f"{max_price if max_price is not None else 'any'}")
    if min_year is not None or max_year is not None:
        print(f"Year filter: {min_year if min_year is not None else 'any'} - "
              f"{max_year if max_year is not None else 'any'}"
              + ("" if include_no_year else ", excluding listings with no year"))
    all_rows, dropped_total = {}, 0
    city_stats, drop_reasons, radius_km = {}, {}, None
    unknown_cities = []
    prev_by_id = {r["item_id"]: dict(r) for r in (previous_rows or [])
                  if r.get("item_id")}
    new_ids, removed, verified_count = [], [], 0
    # Ctrl-C ends a run at whatever point it arrives, and the outputs are
    # written from wherever it got to, so the things they are built from exist
    # before the browser opens.
    interrupted, stopped_during = False, None
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
        try:
            ensure_logged_in(page, timeout_s=login_wait or 600,
                             unattended=unattended)
            first_seg = next(iter(locs.values()))
            radius_km = preflight_pause(
                page, build_search_url(first_seg, queries[0], exact, min_price,
                                       max_price),
                skip=no_pause)
        except KeyboardInterrupt as e:
            # Nothing has been searched yet, so there is nothing to salvage and
            # nothing to leave behind either.
            con.close()
            discard_empty_run_dir(run_dir)
            how = ("The Facebook window was closed" if isinstance(e, WindowClosed)
                   else "Stopped")
            raise SystemExit(f"\n{how} before the sweep started. Nothing was "
                             f"saved.")
        for ci, (label, seg) in enumerate(locs.items()):
            # Facebook's URL takes one query, so several queries means sweeping
            # the city several times. The sightings are merged by listing id
            # before anything is filtered, so a listing found by two queries is
            # one row and costs one description.
            city_rows, per_query, unrecognised = {}, {}, False
            try:
                if ci:
                    # The gap between cities, taken before the city rather than
                    # after it, so that a Ctrl-C during the wait is caught with
                    # the rest of the city's work rather than ending the run.
                    human_pause(6.0, 14.0)
                for qi, q in enumerate(queries):
                    url = build_search_url(seg, q, exact, min_price, max_price)
                    which = (f" query {qi + 1}/{len(queries)}"
                             if len(queries) > 1 else "")
                    print(f"\n[{label}{which}] {url}")
                    graphql_bodies.clear()
                    if not goto_with_retry(page, url):
                        continue
                    human_pause(3.0, 5.0)
                    if city_was_dropped(page, seg):
                        # Sweeping it anyway would file another city's listings
                        # under this name, which looks like coverage and isn't.
                        # The other queries would land in the same wrong city,
                        # so the whole city goes.
                        instead = city_shown(page)
                        print(f"  Facebook doesn't recognise this city, so it "
                              f"searched {instead or 'wherever your account is set to'} "
                              f"instead. Skipping it — remove '{label}' and add "
                              f"it again from a Marketplace URL.")
                        unknown_cities.append({"label": label, "seg": seg,
                                               "searched_instead": instead})
                        unrecognised = True
                        break
                    try:
                        page.wait_for_selector('a[href*="/marketplace/item/"]',
                                               timeout=15000)
                    except Exception:
                        pass  # zero results or slow load; scroll anyway
                    if radius_km is None:
                        radius_km = read_radius_km(page)
                        if radius_km:
                            print(f"  search radius: {describe_radius(radius_km)}")
                    # --keep-all wants the unfiltered tail, so it falls back to
                    # the "no new cards at all" stop instead of the keeper-aware
                    # one.
                    probe = None if keep_all else (
                        lambda c: card_may_keep(c, groups, min_price, max_price))
                    scroll_started = time.time()
                    cards, divider_seen, _dtext, qstats = collect_city(
                        page, scrolls, probe, verbose=True)
                    qstats["scroll_seconds"] = round(time.time() - scroll_started, 1)
                    if qstats["stop_reason"] == STOPPED_BY_HAND:
                        interrupted = True
                    # initial page results are embedded as JSON in script tags.
                    # Skipped on the way out: on Windows a Ctrl-C reaches the
                    # browser too, so the page may already be gone, and the
                    # cards are in hand either way.
                    script_bodies = [] if interrupted else page.eval_on_selector_all(
                        'script[type="application/json"]', SCRIPT_JSON_JS)
                    if debug_dump:
                        dump = debug_root / f"sweep_{seg}"
                        if len(queries) > 1:
                            dump = dump / f"query_{qi + 1}"
                        dump.mkdir(parents=True, exist_ok=True)
                        for i, b in enumerate(graphql_bodies):
                            (dump / f"graphql_{i:03d}.json").write_text(
                                b, encoding="utf-8")
                        (dump / "scripts.json").write_text(
                            json.dumps(script_bodies), encoding="utf-8")
                    json_listings = {}
                    extract_json_listings(script_bodies, json_listings)
                    extract_json_listings(graphql_bodies, json_listings)
                    found = build_rows(cards, divider_seen, json_listings, label,
                                       label_all, groups)
                    for iid, r in found.items():
                        seen = city_rows.get(iid)
                        city_rows[iid] = better_row(seen, r) if seen else r
                    print(f"  {len(cards)} cards in DOM, {len(json_listings)} "
                          f"structured JSON listings, divider "
                          f"{'seen' if divider_seen else 'not seen'}")
                    # Per-scroll accounting, so it is obvious whether the early
                    # stop is saving work or cutting off real results.
                    skipped = scrolls - qstats["scrolls_used"]
                    saved = skipped * qstats["seconds_per_scroll_recent"]
                    qstats.update(query=q, seconds_saved_estimate=round(saved, 1),
                                  divider_seen=divider_seen)
                    print(f"  {qstats['scrolls_used']} of {scrolls} scrolls in "
                          f"{fmt_dur(qstats['scroll_seconds'])} "
                          f"({qstats['stop_reason']}); last new match on scroll "
                          f"{qstats['last_keeper_scroll']}; skipped {skipped} "
                          f"scrolls, saving at least {fmt_dur(saved)}")
                    per_query[q] = qstats
                    if interrupted:
                        break
                    if qi < len(queries) - 1:
                        human_pause(6.0, 14.0)
            except KeyboardInterrupt:
                # Ctrl-C somewhere the sweep doesn't handle for itself: loading
                # a page, a pause between them, reading the radius. This city is
                # kept as far as it got, like any other.
                interrupted = True
                print(f"\n  Stopped during {label}.")
            if interrupted:
                stopped_during = "sweep"
            if not unrecognised and (per_query or city_rows):
                kept = {}
                for iid, r in city_rows.items():
                    ok, why = keep_row(r, exclude, min_price, max_price,
                                       min_year, max_year, include_no_year)
                    if keep_all or ok:
                        kept[iid] = r
                    else:
                        drop_reasons[why] = drop_reasons.get(why, 0) + 1
                dropped = len(city_rows) - len(kept)
                dropped_total += dropped
                print(f"\n  [{label}] kept {len(kept)}, dropped {dropped}"
                      + (f", from {len(per_query)} queries" if len(per_query) > 1
                         else ""))
                city_stats[label] = city_summary(per_query, len(kept), dropped)
                for iid, r in kept.items():
                    all_rows.setdefault(iid, r)
                    storage.upsert(con, r)
                con.commit()
            if interrupted:
                break

        # Only the sweep reads GraphQL bodies. Left attached, the listener
        # would keep collecting them through the hours-long description stage,
        # holding every payload in memory for nothing. Detaching tells the
        # browser to stop sending them, which is a call into a sync API that a
        # Ctrl-C has left wedged — and a stopped run has no description stage to
        # protect from the listener anyway.
        if not interrupted:
            page.remove_listener("response", on_response)
        graphql_bodies.clear()

        rows = list(all_rows.values())
        for r in rows:
            r["_score"] = relevance(r, groups, numbers)
        dup_collapsed = sum(s["kept"] for s in city_stats.values()) - len(rows)
        print(f"\nSwept {len(rows)} unique listings ({dropped_total} dropped, "
              f"{dup_collapsed} duplicates across cities collapsed).")
        scroll_used = sum(s.get("scrolls_used", 0) for s in city_stats.values())
        scroll_possible = sum(s.get("scroll_ceiling", scrolls)
                              for s in city_stats.values())
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
            # Nothing confirms a listing gone but this check, so skipping it on
            # the way out is the safe direction: the listings it would have
            # asked about are carried forward as still up.
            if missing and verifier and not interrupted:
                print(f"Checking {len(missing)} listing"
                      f"{'' if len(missing) == 1 else 's'} that didn't turn up "
                      f"this time...")
                try:
                    removed, verified_count, auth_failed = verifier(ctx, missing)
                except KeyboardInterrupt:
                    interrupted, stopped_during = True, "verifying"
                    removed, verified_count, auth_failed = [], 0, False
                    print("\n  Stopped. Nothing is marked sold on a check that "
                          "didn't finish.")
                if auth_failed:
                    raise SessionExpired(
                        "The Facebook session expired while checking listings.")
                if not interrupted:
                    print(f"  {len(removed)} confirmed sold or taken down, "
                          f"{verified_count - len(removed)} still up.")
            new_ids, carried = storage.reconcile_with_previous(
                all_rows, prev_by_id, {r["item_id"] for r in removed},
                score=lambda r: relevance(r, groups, numbers))
            if carried:
                print(f"  {carried} kept from previous runs (not in this feed, "
                      f"but not confirmed gone either).")
            rows = list(all_rows.values())

        thumbs_path = Path(thumbs_dir)
        if not thumbs_path.is_absolute():
            thumbs_path = out_path.resolve().parent / thumbs_path

        described, described_ids = 0, []
        if do_descriptions and rows and not interrupted:
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
            if targets:
                # Commit per listing: this stage runs for as long as an hour and
                # is the one people interrupt, so nothing may sit in memory
                # waiting for the stage to finish.
                def save_row(r):
                    storage.upsert(con, r)
                    con.commit()
                finished = retrieve_descriptions(ctx, page, targets,
                                       thumbs_path if do_thumbs else None,
                                       debug_dump, pace, on_row=save_row)
                described = sum(1 for r in targets if r.get("description"))
                described_ids = [r["item_id"] for r in targets if r.get("description")]
                # An interrupt means stop working, not throw away the run: skip
                # the remaining downloads and go straight to the CSV + gallery.
                if not finished:
                    interrupted, stopped_during = True, "descriptions"

        if do_thumbs and rows and not interrupted:
            try:
                fetch_thumbs(ctx, rows, thumbs_path)
            except KeyboardInterrupt:
                interrupted, stopped_during = True, "thumbnails"
                print("\n  Stopped. Keeping the photos already downloaded.")
            for r in rows:
                storage.upsert(con, r)
            con.commit()
        # Not after a Ctrl-C. Raised out of a Playwright call, it leaves the
        # sync API wedged, and closing the context is a call into it that never
        # returns — no exception, no timeout, just a run that stops one step
        # short of writing everything it spent the last hour gathering. Leaving
        # the block below stops the driver, and that takes the browser with it,
        # which is all closing would have achieved.
        if not interrupted:
            try:
                ctx.close()
            except Exception:
                pass
    con.close()
    if interrupted and not rows:
        print("\nStopped before anything was found. Nothing was saved.")
        discard_empty_run_dir(run_dir)
        return {"status": "error",
                "error": "Stopped before the sweep found anything."}
    if interrupted:
        print(f"\nStopped during {STAGE_NAMES.get(stopped_during, 'the run')}. "
              f"Writing the {len(rows)} listing"
              f"{'' if len(rows) == 1 else 's'} already found, then the "
              f"gallery.")
    storage.write_csv(rows, out_path)
    print(f"\nWrote {out_path} and {storage.DB_PATH}")
    gallery, gallery_path, light_gallery = None, None, None
    if do_gallery and rows:
        try:
            import build_gallery
            gallery_path = Path(build_gallery.build(out_path))
            gallery = gallery_path.name
            # The same catalogue, but pointing at the photos in thumbnails/
            # rather than carrying them. A fraction of the size, at the cost of
            # only working while it sits in the run folder.
            light_gallery = Path(build_gallery.build(
                out_path, out_path.with_name("lightweight_gallery.html"),
                embed=False)).name
        except KeyboardInterrupt:
            # The one stage where stopping costs nothing but the wait: the CSV
            # it reads is already on disk.
            print(f"\nStopped during the gallery. The results are in "
                  f"{out_path}; to build it later, run: "
                  f"python3 src/build_gallery.py {out_path}")
        except Exception as e:
            print(f"Gallery step failed ({e}). Run: "
                  f"python3 src/build_gallery.py {out_path}")
    elapsed = time.time() - started
    if run_dir:
        manifest = {
            "query": label_all,
            "queries": queries,
            "started": started_iso,
            "finished": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "duration_seconds": round(elapsed, 1),
            "duration_human": fmt_dur(elapsed),
            "settings": {
                "exact": exact, "scrolls": scrolls, "pace": pace,
                "min_price": min_price, "max_price": max_price,
                "min_year": min_year, "max_year": max_year,
                "include_no_year": include_no_year,
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
                # Per city per query: a city is scrolled once for each query.
                "ceiling_per_city": scrolls,
                "queries": len(queries),
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
            "interrupted": interrupted,
            "interrupted_during": stopped_during,
            "images_local": sum(1 for r in rows
                                if (r.get("image") or "").startswith(f"{thumbs_path.name}/")),
            "files": {"csv": out_path.name, "gallery": gallery,
                      "lightweight_gallery": light_gallery,
                      "thumbnails": thumbs_path.name,
                      "database": str(storage.DB_PATH)},
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
        show_gallery(gallery_path)
    return {
        "status": "ok",
        "query": label_all,
        "queries": queries,
        "run_dir": str(run_dir) if run_dir else None,
        "csv": str(out_path),
        "gallery": str(gallery_path) if gallery_path else None,
        "lightweight_gallery": (str(out_path.with_name(light_gallery))
                                if light_gallery else None),
        "started": started_iso,
        "finished": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_seconds": round(elapsed, 1),
        "new_ids": new_ids,
        # Only listings this sweep actually saw. total_ids also carries forward
        # old listings that merely weren't confirmed gone, and those must not be
        # recorded as "seen alive" or they'd never be re-checked.
        "feed_ids": sorted(feed_ids),
        "total_ids": [r["item_id"] for r in rows],
        "new_rows": [r for r in rows if r["item_id"] in set(new_ids)],
        "removed": removed,
        "listings_verified": verified_count,
        "described_ids": described_ids,
        "descriptions_fetched": described,
        "interrupted": interrupted,
        "interrupted_during": stopped_during,
        "radius_km": radius_km,
        "per_city": city_stats,
        "locations": locs,
        "unknown_cities": unknown_cities,
    }


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
    out_fields = list(dict.fromkeys(list(rows[0].keys()) if rows else storage.FIELDS))
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
    storage.write_csv(uniq, out, out_fields)
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
    storage.write_csv(uniq, out, list(uniq[0].keys()) if uniq else storage.FIELDS)
    print(f"Wrote {out}. Finished in {fmt_dur(time.time() - started)}.")


def login_only():
    """Refresh the saved Facebook session and nothing else.

    The session lives in this app's own browser profile, so logging in with Safari
    or Chrome does nothing for it. Closing the context is what writes the session
    to disk, so this waits for a clean shutdown rather than leaving it to an
    abandoned sweep."""
    print("Opening Facebook. If you're already logged in, this finishes by itself.")
    with sync_playwright() as p:
        ctx = launch_context(p, notice=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        ensure_logged_in(page)
        # Closing the context is what writes the session to the browser profile.
        ctx.close()
    print("\nLogged in, and the session is saved. Searches — including scheduled "
          "ones — will use it until Facebook expires it, usually a few weeks.")


def set_radius():
    """The Marketplace search radius is an account setting, not a URL parameter,
    so this opens the UI and waits for you to change it — then confirms the new
    value from the page's own filter payload. Normally you want this pinned at
    the 500-mile maximum, which is what the saved city spacing is built around."""
    seg = next(iter(locations.load_locations().values()))
    with sync_playwright() as p:
        ctx = launch_context(p, notice=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        ensure_logged_in(page)
        page.goto(build_search_url(seg, "test", False), wait_until="domcontentloaded")
        human_pause(3.0, 5.0)
        before = read_radius_km(page)
        print(f"\nCurrent radius: {describe_radius(before) or 'unknown'}")
        if before == EXPECTED_RADIUS_KM:
            print("That's the 500-mile maximum — the widest Facebook will "
                  "search around each city. No change needed unless you want "
                  "it smaller.")
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


def why_wait(e):
    """What to say when the run lock is already held.

    Usually it's another sweep, and the reason is the shared Facebook session.
    An update takes the same lock for a very different reason, so the sentence
    explaining it has to know which one it's looking at.
    """
    if (getattr(e, "holder", None) or {}).get("what") == "an update":
        return ("An update is replacing this app's files, so nothing can start "
                "until it's finished. That only takes a minute.")
    return ("Both runs would need the same Facebook session, so wait for that "
            "one to finish.")


# What the launchers read as "closed, with nothing in the terminal worth
# reading" — the settings window shut without starting anything. They skip their
# "press Return to close this window" on it, so quitting the app quits the app
# rather than leaving a terminal behind to be dismissed as well.
CLOSED_EXIT = 76

# How long the settings window is given to finish drawing itself before the
# gallery is opened over it. Only cosmetic: without it the browser can win the
# race and end up behind a window that appeared afterwards.
SETTLE_MS = 400


def run_from_ui(a):
    """The settings window, and every search started from it, until it's closed.

    That window is the app as far as anyone using it is concerned, so a finished
    search comes back to it rather than ending the program: the window opens
    again and the results open in the browser on top of it, which puts the
    listings in front and leaves the app ready for the next search underneath.
    Closing the window is how the app is quit — see CLOSED_EXIT.

    Command-line values seed the form, so --query x --ui opens it pre-filled;
    after that the form comes back holding whatever was last searched for.
    """
    import settings_ui
    import scheduling
    import make_desktop_icon
    import past_runs
    locs = {}

    def ui_add_city(label, text):
        updated, err = locations.add_location(label, text)
        return (list(updated.keys()) if updated else list(locs)), err

    def ui_remove_city(label):
        updated, err = locations.remove_location(label)
        return list(updated.keys()), err

    defaults = {"queries": query_list(a.query), "exclude": a.exclude or "",
                "pace": a.pace, "max_queries": MAX_QUERIES,
                "page_work": PAGE_WORK_SECONDS, "photo_save": PHOTO_SAVE_SECONDS}
    gallery = None
    while True:
        # A city added last time round is in the list this time.
        locs = locations.load_locations()
        shown = []

        def ready(page, path=gallery):
            page.wait_for_timeout(SETTLE_MS)
            shown.append(path)
            show_gallery(path)

        try:
            cfg = settings_ui.collect_settings(
                list(locs.keys()), PACES, defaults,
                on_add=ui_add_city, on_remove=ui_remove_city,
                builtins=list(locations.base_locations()),
                on_ready=ready if gallery else None,
                # Four unrelated sets of hooks: the scheduled searches and email
                # tabs, the runs already on disk, the offer of a desktop shortcut
                # on a launch that hasn't one yet, and the offer of a newer
                # version when this copy is behind the repository.
                hooks={**scheduling.ui_hooks(), **past_runs.ui_hooks(),
                       **make_desktop_icon.ui_hooks(), **updater.ui_hooks()})
        finally:
            # Results nobody asked to wait for. A window that couldn't open is a
            # reason to say where the last search went, not to swallow it.
            if gallery and not shown:
                show_gallery(gallery)
        gallery = None
        # The window was closed, or Cancel was pressed. Nothing is running and
        # nothing is half-finished, so there's nothing to keep the app open for.
        if not cfg:
            raise SystemExit(CLOSED_EXIT)
        # "Run now" on a scheduled search has to wait for this window to close,
        # because the window is holding the one Chromium profile the session
        # lives in. Its results go out by email, so there's no gallery to open.
        if cfg.get("action") == "run_saved":
            scheduling.tick(force=cfg["id"])
            continue
        # The code on disk is newer than the code this process loaded, so there's
        # nothing safe left to do here. Exiting with the launcher's code is what
        # gets the app started again on the version that was just installed.
        if cfg.get("action") == "updated":
            again = updater.relaunch_code()
            if again is None:
                print("Updated. Start Faceplace Marketbook again to use the new "
                      "version.")
                return
            print("Updated. Restarting on the new version...")
            raise SystemExit(again)
        defaults = {**defaults, "queries": query_list(cfg.get("queries")),
                    "exclude": cfg.get("exclude") or "", "pace": cfg["pace"]}
        asked_for = " OR ".join(f"'{q}'" for q in query_list(cfg.get("queries")))
        print(f"\nStarting: "
              f"{'queries' if len(cfg.get('queries') or []) > 1 else 'query'} "
              f"{asked_for}, {len(cfg['cities'])} "
              f"cit{'y' if len(cfg['cities']) == 1 else 'ies'}.")
        try:
            with scheduling.run_lock("a manual run"):
                summary = run(
                    cfg["queries"], DEFAULT_SCROLLS, cfg["exact"], a.out, None,
                    a.keep_all, cfg["debug_dump"], a.match, cfg["limit"],
                    a.thumbs_dir,
                    # No do_gallery: the window doesn't offer to skip it. A run
                    # whose results can only be read as a CSV is a run nobody
                    # wanted, and it costs seconds at the end of an hour.
                    do_descriptions=cfg["do_descriptions"],
                    do_thumbs=cfg["do_thumbs"], pace=cfg["pace"],
                    exclude=[t.strip() for t in cfg["exclude"].split(",")
                             if t.strip()],
                    min_price=cfg["min_price"], max_price=cfg["max_price"],
                    min_year=cfg["min_year"], max_year=cfg["max_year"],
                    include_no_year=cfg["include_no_year"],
                    only_labels=cfg["cities"],
                    # Held back until the window is up again, so the gallery
                    # lands in front of it rather than behind it.
                    open_gallery=False, no_pause=a.no_pause)
        except scheduling.AlreadyRunning as e:
            print(f"\nNot starting: {e}.\n{why_wait(e)}")
            continue
        gallery = None if a.no_open else (summary or {}).get("gallery")


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
    ap.add_argument("--query", action="append", metavar="TEXT",
                    help=f"search query (required for a run). Repeat it for up "
                         f"to {MAX_QUERIES} queries: a listing is kept if it "
                         f"matches every word of any one of them, and each one "
                         f"is a separate sweep of every city")
    ap.add_argument("--import-urls", metavar="FILE")
    ap.add_argument("--out", metavar="CSV",
                    help="explicit CSV path; skips the per-run runs/<query>_<date>/ folder")
    ap.add_argument("--exclude", metavar="TERMS", default="",
                    help="comma-separated terms to reject, matched at word starts "
                         "ignoring case and punctuation, so 'can am' also kills "
                         "'Can-Am' and 'CAN AM' (but not 'canam'), and 'fender' "
                         "spares 'Defender'")
    ap.add_argument("--min-price", type=int, metavar="N",
                    help="drop listings under N dollars (also sent to Facebook)")
    ap.add_argument("--max-price", type=int, metavar="N",
                    help="drop listings over N dollars (also sent to Facebook)")
    ap.add_argument("--min-year", type=int, metavar="Y",
                    help="drop listings whose title year is before Y")
    ap.add_argument("--max-year", type=int, metavar="Y",
                    help="drop listings whose title year is after Y")
    ap.add_argument("--exclude-no-year", action="store_true",
                    help="with a year bound set, also drop listings whose title "
                         "has no year in it at all")
    ap.add_argument("--no-pause", action="store_true",
                    help="skip the pause after login for popups and the radius")
    ap.add_argument("--no-open", action="store_true",
                    help="don't open the finished gallery in a browser")
    ap.add_argument("--set-radius", action="store_true",
                    help="open Marketplace so you can check/change the account search radius")
    ap.add_argument("--login", action="store_true",
                    help="log into Facebook and save the session, without running a search")
    ap.add_argument("--desktop-icon", action="store_true",
                    help="put a double-clickable icon on the desktop, then exit")
    ap.add_argument("--ui", action="store_true",
                    help="open the settings window (the default when run with no arguments)")
    ap.add_argument("--no-ui", action="store_true",
                    help="never open the settings window, even with no arguments")
    ap.add_argument("--only", metavar="LABEL", help="run only locations whose label contains LABEL")
    ap.add_argument("--match", metavar="TERM", help="only describe listings whose title contains TERM")
    ap.add_argument("--limit", type=int, metavar="N",
                    help="only retrieve descriptions for the first N listings")
    ap.add_argument("--thumbnails-dir", default=THUMBS_DIRNAME,
                    dest="thumbs_dir", metavar="DIR",
                    help=f"thumbnail folder (default: {THUMBS_DIRNAME})")
    ap.add_argument("--pace", choices=list(PACES), default=DEFAULT_PACE,
                    help="pause between detail-page hits while retrieving "
                         "descriptions: "
                         "fast (1-2.5s, ~7s per listing, default), "
                         "slow (3-5s, ~9s per listing)")
    ap.add_argument("--scrolls", type=int, default=DEFAULT_SCROLLS,
                    help=f"max scrolls per city (safety ceiling, default {DEFAULT_SCROLLS}; "
                         f"normally stops much sooner, after {KEEPER_PATIENCE} scrolls "
                         "with no new matches)")
    ap.add_argument("--exact", action="store_true", help="tight matching (default loose)")
    ap.add_argument("--keep-all", action="store_true",
                    help="keep outside-search and non-matching listings instead of dropping them")
    ap.add_argument("--no-descriptions", action="store_true",
                    help="skip the detail-page stage")
    ap.add_argument("--no-thumbs", action="store_true", help="skip downloading images")
    ap.add_argument("--no-gallery", action="store_true", help="skip building gallery.html")
    ap.add_argument("--debug-dump", action="store_true",
                    help="save raw Facebook JSON payloads to debug/ for troubleshooting")
    ap.add_argument("--descriptions", metavar="CSV",
                    help="one-off: retrieve descriptions for an existing CSV "
                         "instead of running a sweep")
    ap.add_argument("--download-thumbs", metavar="CSV",
                    help="one-off: download the image URLs in an existing CSV")
    a = ap.parse_args()
    # A range that can't match anything fails here rather than an hour later, as
    # a sweep that scrolled every city and came back with nothing.
    for flag, val in (("--min-price", a.min_price), ("--max-price", a.max_price)):
        if val is not None and val < 0:
            ap.error(f"{flag} can't be negative")
    if None not in (a.min_price, a.max_price) and a.min_price > a.max_price:
        ap.error("--min-price is higher than --max-price")
    for flag, val in (("--min-year", a.min_year), ("--max-year", a.max_year)):
        if val is not None and not EARLIEST_YEAR <= val <= latest_year():
            ap.error(f"{flag} has to be between {EARLIEST_YEAR} and {latest_year()}")
    if None not in (a.min_year, a.max_year) and a.min_year > a.max_year:
        ap.error("--min-year is later than --max-year")
    if len(query_list(a.query)) > MAX_QUERIES:
        ap.error(f"--query can be given at most {MAX_QUERIES} times")
    # One line, and only when there's something to say. The settings window
    # makes the same offer with a button beside it; this is for the runs that
    # never open one. Both read the same answer, which is fetched once per
    # launch, so having it here costs the window nothing.
    updater.announce()
    exclude = [t.strip() for t in a.exclude.split(",") if t.strip()]
    if a.import_urls:
        locations.import_urls(a.import_urls)
    elif a.desktop_icon:
        import make_desktop_icon
        # An explicit argument list: this script's own flags aren't its.
        make_desktop_icon.main([])
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
        # The same lock the scheduler and the settings window take: two sweeps
        # can't share the one Facebook session, so a scheduled run starting
        # mid-sweep would crash both.
        import scheduling
        try:
            with scheduling.run_lock("a manual run"):
                run(a.query, a.scrolls, a.exact, a.out, a.only, a.keep_all,
                    a.debug_dump, a.match, a.limit, a.thumbs_dir,
                    do_descriptions=not a.no_descriptions, do_thumbs=not a.no_thumbs,
                    do_gallery=not a.no_gallery, pace=a.pace, exclude=exclude,
                    min_price=a.min_price, max_price=a.max_price,
                    min_year=a.min_year, max_year=a.max_year,
                    include_no_year=not a.exclude_no_year,
                    open_gallery=not a.no_open, no_pause=a.no_pause)
        except scheduling.AlreadyRunning as e:
            raise SystemExit(f"Not starting: {e}.\n{why_wait(e)}")


if __name__ == "__main__":
    try:
        main()
    except WindowClosed as e:
        # Every stage that has something to save catches this for itself. Reaching
        # here means one that hadn't — logging in, or setting the radius — so all
        # that's left is to say so plainly instead of printing a traceback.
        raise SystemExit(f"\n{e}")
