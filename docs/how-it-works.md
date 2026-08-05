# How it works

Internals, command-line usage, and the measurements behind the advice in the
[README](../README.md). Nothing here is needed to use the app.

## Layout

- `fb_marketplace_sweep.py` — sweep, enrich, thumbnails, and the pipeline that
  runs them in order.
- `settings_ui.py` — the pre-flight settings window. It's HTML rendered in a
  Playwright window, so there's nothing extra to install and it matches the
  gallery's styling.
- `build_gallery.py` — turns a results CSV into a browsable `gallery.html`.
- `locations.json` — hand-curated `label -> search segment` map.
- `Start Faceplace (Mac).command` / `(Windows).bat` — double-click launchers.
  They find Python, build `.venv`, install `requirements.txt`, fetch Chromium,
  and run the app. All work is stamped so repeat runs skip straight through.
- `runs/` — one folder per run (git-ignored).
- `marketplace_results.sqlite` — cumulative archive across all runs
  (git-ignored). Listings are upserted, so re-seeing one updates its price,
  title, and `scraped_at`.
- `.fb_session/` — browser profile holding the login session (git-ignored,
  and it must stay that way).

## Requirements

Python 3.9 or newer, and one third-party package (`playwright`). 3.9 is a real
floor, not a guess: the launchers refuse anything older, and the whole app is
tested against 3.9. Chromium is downloaded by Playwright itself.

## Running it from a terminal

The launchers exist for non-technical use. Directly:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
.venv/bin/python fb_marketplace_sweep.py            # opens the settings window
```

Or skip the window entirely:

```bash
.venv/bin/python fb_marketplace_sweep.py --query "land rover defender" \
    --exclude "can am, otterbox" --min-price 4000 --max-price 120000
```

`--ui` forces the window even when flags are given, seeding the form with them.
`--no-ui` always goes straight to the sweep.

### Locations

Location handling uses search URLs grabbed by hand, so there's no fragile
city-picker automation. Paste them into a text file, one per line, optionally
prefixed with a label and a comma:

```
Dallas, TX, https://www.facebook.com/marketplace/dallas/search/?query=...
https://www.facebook.com/marketplace/108173265878171/search/?query=...
```

Then `--import-urls locations_urls.txt` parses them into `locations.json`. A
slug like `dallas` and a numeric id both work.

### Options

| Flag | Description |
| --- | --- |
| `--query TEXT` | Search query. Without it (and without `--no-ui`) the settings window opens. |
| `--ui` / `--no-ui` | Force the settings window on / off. |
| `--import-urls FILE` | Parse pasted search URLs into `locations.json`. |
| `--out CSV` | Explicit CSV path; skips the per-run `runs/<query>_<date>/` folder. |
| `--only LABEL` | Run only locations whose label contains `LABEL`. |
| `--exclude TERMS` | Comma-separated terms to reject, ignoring case, spaces, and punctuation. |
| `--min-price N` / `--max-price N` | Price bounds in dollars, applied server-side and locally. |
| `--enrich-budget MIN` | Ask before enriching longer than `MIN` minutes. Default `0`, which never asks. |
| `--yes` | Don't ask about long enrichment jobs. |
| `--no-open` | Don't open the finished gallery in a browser. |
| `--set-radius` | Open Marketplace to check or restore the account search radius. |
| `--match TERM` | Only enrich listings whose title contains `TERM`. |
| `--limit N` | Only enrich the first `N` listings. |
| `--thumbs-dir DIR` | Thumbnail folder (default: `thumbs`). |
| `--pace NAME` | Pause between detail-page hits while enriching: `fast` (1-2.5s, ~7s per listing, default) or `slow` (3-5s, ~9s). |
| `--scrolls N` | Max scrolls per city — a safety ceiling only; the sweep normally stops much sooner, after 3 scrolls with no new matches. Default `60`. |
| `--exact` | Ask Facebook for tight matching (default is loose). |
| `--keep-all` | Keep filler/non-matching listings instead of dropping them (flagged in `source_section` / `matches_query`). |
| `--no-enrich` / `--no-thumbs` / `--no-gallery` | Skip that stage. |
| `--debug-dump` | Save raw Facebook JSON payloads to `debug/` for troubleshooting extraction. |
| `--enrich CSV` | One-off: enrich an existing CSV instead of running a sweep. |
| `--download-thumbs CSV` | One-off: download the image URLs in an existing CSV. |

## The four stages

1. **Sweep** — visit each saved location and scroll its results, filtering as it
   goes and stopping once three scrolls in a row turn up nothing that could pass
   the filters (see [How deep it scrolls](#how-deep-it-scrolls)).
2. **Enrich** — visit each kept listing's detail page *once* for the seller's
   description and full-size photo. Each listing is written to the database the
   moment it's done, and Ctrl-C stops the work without discarding it: the run
   skips the remaining downloads and goes straight to the CSV and gallery.
3. **Thumbnails** — save every image locally. The enrich stage stores each photo
   the moment it reads the URL, so nothing expires mid-run; anything already on
   disk is reused.
4. **Gallery** — write `gallery.html` and open it in a browser.

Each detail page is visited at most once per run and each image downloaded at
most once: results from every city are merged by listing id *before* the enrich
and thumbnail stages, so overlapping city radii never cause duplicate work.

## How extraction works

Most durable first:

1. **Structured JSON.** Facebook ships the data it renders from — GraphQL
   responses captured while scrolling, plus embedded
   `<script type="application/json">` blobs in the initial page. These carry
   typed fields (`marketplace_listing_title`, `listing_price.formatted_amount`,
   location, mileage, photo URI, full description) and survive markup churn far
   better than CSS classes, which are all machine-generated.
2. **DOM fallback.** Result-card anchors matched by the
   `/marketplace/item/<id>` href pattern, with line-classification heuristics
   for price, location, mileage, and title. Used to establish page order and to
   fill gaps.

If a sweep suddenly returns empty fields, run with `--debug-dump` and inspect
the saved payloads in `debug/`.

## Filtering

In order of how much junk they remove:

- **`--exclude`** is the big one when the search term is an overloaded brand
  name. Terms match with punctuation and spaces ignored, so one `can am` entry
  catches "Can-Am", "Can Am", and "CANAM". On a real "defender 110" sweep this
  alone cut 4,698 listings to 1,925, nearly all of it Can-Am UTVs.
- **`--min-price` / `--max-price`** are sent to Facebook in the URL *and*
  applied locally. A price floor removes the parts and accessories that
  legitimately say "Defender 110": shocks, doors, a $75 window, a $1
  transmission. Took that same sweep from 1,925 to 811. Listings with no price
  are kept.
- **Query words** are required by default: 3+ letter words, matched at word
  starts, so "defender" catches "Defenders" but "van" won't match "advantage".
- **Numbers in the query rank rather than filter.** Sellers often omit them, so
  a "110" isn't required — but listings that have it are enriched first, along
  with those whose titles contain every query word. An interrupted or capped
  enrich run therefore spends its time on the best candidates.

### Why `--exact` is off by default

Measured head-to-head on one city with everything else identical. `exact=true`
returned 63 raw cards versus 1,599, but after filtering yielded only 39 listings
against 73 — and every one of its 39 was already in the loose set. It found
nothing new while discarding 34 genuine Defender 110s, including a $30,995 2022
and an $86,992 2025. It's a fast reconnaissance mode, not a better one.

## Search radius

The radius is an **account-level Marketplace setting**, not a URL parameter —
none of the obvious URL spellings affect it. Every sweep prints the radius it
found and warns if it's under 500 miles, since the saved cities are spaced so
that a 500-mile circle around each tiles the continental US. If it gets reset,
coverage develops holes; `--set-radius` opens the UI and waits while you fix it.

A side effect worth knowing: at 500 miles almost nothing is "outside your
search", so Facebook's out-of-radius divider rarely appears (1 city in 12 on a
real run). The script still detects it and stops scrolling when it does, but it
isn't a meaningful filter at this radius.

## How deep it scrolls

Facebook's result feed never really ends. Once it runs out of real matches it
pads indefinitely with loosely related inventory, so "keep scrolling until there
are no new listings" almost never triggers — the sweep just ran to its 60-scroll
ceiling every time, collecting ~1,700 cards per city to keep ~80.

So the filters run *inside* the scroll loop. Each newly seen card is tested
against the same rules that decide what gets kept, and the sweep stops after
`KEEPER_PATIENCE` (3) consecutive scrolls that produce no card capable of
passing. The in-loop test is deliberately generous — a card whose text hasn't
rendered yet counts as a match — so a slow-loading page can't cut a city short.
It's also dedupe-blind: a listing another city already found still counts as new
here, which biases toward scrolling slightly too long rather than too little.

This matters more than it looks, because each snapshot re-reads every card
already on the page: scroll 50 costs several times what scroll 5 does. Stopping
when the matches dry up skips exactly the most expensive scrolls.

Each city reports where it stopped and why, and `run.json` records it under
`per_city` and `scrolling`:

```
  scroll 11: +31 cards, +0 matches (486 cards / 74 matches so far)
  3 scrolls with no new matches after scroll 12; stopping scroll
  12 of 60 scrolls in 4m 2s (no new matches); last new match on scroll 9;
  skipped 48 scrolls, saving at least 21m 36s
```

## Pacing

Enrichment is the slow stage, and most of its cost is a deliberate randomized
pause between detail-page hits — the one knob that trades runtime against how
machine-like the traffic looks. `--pace fast` (default) waits 1-2.5s, landing
near 7s per listing; `--pace slow` waits 3-5s for about 9s.

Neither can go below the fixed per-listing cost: ~3.5s to load a detail page and
read its payload, plus ~1.5s to fetch and store the photo when thumbnails are
on. That floor is why "fast" saves less than the pause numbers suggest.

The estimate is always printed before enrichment starts. `--enrich-budget MIN`
makes anything longer than that stop and offer the top N by relevance, all of
them, or none. It's `0` by default, which never asks.

## The gallery

A normal run builds it. To rebuild from a CSV without re-scraping:

```bash
python3 build_gallery.py runs/<folder>/results.csv
```

It's one portable file: thumbnails are baked in as data URIs, so it needs no
`thumbs/` folder, no server, and no live Facebook URLs. `--no-embed` links
thumbnails by path instead and keeps the file small (a few hundred KB rather
than ~10 MB per 150 listings), but then it only works from its original folder
with `thumbs/` beside it, opened directly in a browser rather than a preview
pane.

Features: client-side text search, a searched-city filter, price/title sorting,
and a click-through detail view with the full description.

Hiding: each card has an `✕` in its corner (visible on hover). The tally line
then offers "show N hidden" to review or un-hide. Hidden listings are remembered
by listing id in the browser's local storage, so the state survives filtering,
sorting, reloads, and even rebuilding the file from a later sweep. If the
browser blocks local storage for local files, hiding works for the session but
won't persist.

Fonts come from Google Fonts when online — Lato for body text, Courier Prime for
the masthead and UI type. Offline, each falls back to a system face. To swap one,
edit the `--brandface` or `--type` variable at the top of the stylesheet in
`build_gallery.py`.

## Keeping the machine awake

A sleeping laptop drops the browser connection, which costs whatever hasn't been
written to disk. `keep_awake()` wraps each long-running stage and asks the OS to
stay up: `caffeinate -ims -w <pid>` on macOS (the `-w` ties its lifetime to ours,
so it can't outlive the run even if we're killed), and
`SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)` via `ctypes` on
Windows. Both are released in a `finally`. Any failure to arrange it is
swallowed — the run proceeds as it did before.

Neither mechanism can defeat closing a laptop lid, which is a hardware-level
sleep. `set_radius()` is deliberately not wrapped, since it exists to wait on a
human.

## Gatekeeper

Every Mac user hits this once, and it isn't the usual quarantine problem — a
`git clone`'d copy with no `com.apple.quarantine` attribute is blocked just the
same, because the launcher is simply unsigned (`spctl` reports
`no usable signature`). macOS 15 removed the Control-click → Open bypass for
unsigned software, so the only routes are System Settings → Privacy & Security →
**Open Anyway**, or running the script from Terminal, where Gatekeeper doesn't
apply. Both are in the README.

The only way to remove that friction is a paid Apple Developer account plus
notarization, which is out of scope for a personal tool.

## Cross-platform notes

- All paths are derived from the script's own location via `pathlib`, so the app
  doesn't care about the working directory.
- Both launchers set `PYTHONUTF8=1`, and every file read and write names its
  encoding explicitly, so accented listing titles behave the same regardless of
  a machine's regional settings.
- `.gitattributes` pins `*.bat` to CRLF and `*.command` to LF. A batch file with
  Unix line endings can misbehave on Windows, and the `.command` file must keep
  its executable bit to stay double-clickable.
