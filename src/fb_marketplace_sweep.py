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
from browser import (SessionExpired, ensure_logged_in, fmt_dur, goto_with_retry,
                     human_pause, keep_awake, launch_context)
from descriptions import (DEFAULT_DESCRIPTIONS_BUDGET_MIN, DEFAULT_PACE, PACES,
                          PAGE_WORK_SECONDS, PHOTO_SAVE_SECONDS,
                          confirm_description_count, fetch_thumbs,
                          retrieve_descriptions)
from listings import (ITEM_RE, SCRIPT_JSON_JS, build_rows, card_may_keep,
                      extract_json_listings, keep_row, query_numbers,
                      query_tokens, relevance)

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
    except KeyboardInterrupt:
        raise SystemExit("\nStopped before the sweep started. Nothing was saved.")

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
    overrides it. The SQLite database is the cumulative index across every run
    and lives outside the run folders.

    A scheduled saved search passes the extra arguments: run_dir to write into
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
    tokens = query_tokens(query)
    numbers = query_numbers(query)
    if run_dir:
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        out_path = run_dir / "results.csv"
    elif out_csv:
        out_path, run_dir = Path(out_csv), None
    else:
        run_dir = storage.make_run_dir(query)
        out_path = run_dir / "results.csv"
    con = storage.open_db(storage.DB_PATH)
    debug_root = (run_dir / "debug") if run_dir else paths.DEBUG_DIR
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
                storage.upsert(con, r)
            con.commit()
            human_pause(6.0, 14.0)

        # Only the sweep reads GraphQL bodies. Left attached, the listener
        # would keep collecting them through the hours-long description stage,
        # holding every payload in memory for nothing.
        page.remove_listener("response", on_response)
        graphql_bodies.clear()

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
            new_ids, carried = storage.reconcile_with_previous(
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
                    storage.upsert(con, r)
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
                storage.upsert(con, r)
            con.commit()
        try:
            ctx.close()
        except Exception:
            pass
    con.close()
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
        except Exception as e:
            print(f"Gallery step failed ({e}). Run: "
                  f"python3 src/build_gallery.py {out_path}")
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
        ctx = launch_context(p)
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
        ctx = launch_context(p)
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


def run_from_ui(a):
    """Collect settings from the pre-flight window, then run with them.
    Command-line values seed the form, so --query x --ui opens it pre-filled."""
    import settings_ui
    import scheduling
    import make_desktop_icon
    locs = locations.load_locations()

    def ui_add_city(label, text):
        updated, err = locations.add_location(label, text)
        return (list(updated.keys()) if updated else list(locs)), err

    def ui_remove_city(label):
        updated, err = locations.remove_location(label)
        return list(updated.keys()), err

    cfg = settings_ui.collect_settings(
        list(locs.keys()), PACES,
        {"query": a.query or "", "exclude": a.exclude or "", "pace": a.pace,
         "page_work": PAGE_WORK_SECONDS, "photo_save": PHOTO_SAVE_SECONDS,
         "descriptions_budget": a.descriptions_budget},
        on_add=ui_add_city, on_remove=ui_remove_city,
        builtins=list(locations.base_locations()),
        # Two unrelated sets of hooks: the saved searches and email tabs, and
        # the offer of a desktop shortcut on a launch that hasn't one yet.
        hooks={**scheduling.ui_hooks(), **make_desktop_icon.ui_hooks()})
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
    ap.add_argument("--descriptions-budget", type=int, metavar="MIN",
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
                    descriptions_budget=a.descriptions_budget, assume_yes=a.yes,
                    open_gallery=not a.no_open, no_pause=a.no_pause)
        except scheduling.AlreadyRunning as e:
            raise SystemExit(
                f"Not starting: {e}.\nBoth runs would need the same Facebook "
                f"session, so wait for that one to finish.")


if __name__ == "__main__":
    main()
