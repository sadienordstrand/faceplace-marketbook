# How it works

Internals, command-line usage, and the measurements behind the advice in the
[README](../README.md). Nothing here is needed to use the app.

## Layout

The only things at the top are the four files meant to be double-clicked, the
README, and folders:

```
Start Faceplace Marketbook (Mac).command      Log into Facebook (Mac).command
Start Faceplace Marketbook (Windows).bat      Log into Facebook (Windows).bat
README.md      requirements.txt
src/     tests/     docs/     runs/
hidden:  .state/   .venv/   .gitignore   .gitattributes
```

`src/paths.py` distinguishes the two folders that are easy to confuse, and it's
the only place that does: **`CODE_DIR`** is `src/`, where the modules and their
assets live, and **`ROOT`** is the project folder someone opens in Finder,
holding the launchers, the virtualenv, and everything the app writes. The
distinction is load-bearing — the scheduler's plist names `src/scheduling.py`,
but the "is this in a folder macOS guards?" check is about the project folder —
and getting it backwards is a bug that only shows up on someone else's machine.

### `src/`

The sweep is six modules. `fb_marketplace_sweep.py` is the entry point and the
sweep proper: search URLs, the search radius, scrolling a city and knowing when
to stop, the `run()` pipeline that drives every stage in one browser session,
and the command line.

The other five are the pieces it drives, each usable and testable on its own:

- `browser.py` (Chromium, the profile holding the login, navigation retries,
  pacing)
- `locations.py` (the cities, and parsing a pasted Marketplace URL)
- `listings.py` (page to rows, and which rows survive the filters)
- `storage.py` (the CSV, the SQLite archive, the run folders)
- `descriptions.py` (the detail-page pass and its photos)

Alongside them:

- `settings_ui.py`
- `build_gallery.py`
- `past_runs.py`
- `scheduling.py`
- `make_desktop_icon.py`
- `updater.py` and `version.py`
- `paths.py`
- `ui/`
  - `settings.html`, `settings.css`, and `settings.js`
  - `update.html`, `update.css`, and `update.js`
  - `gallery.html`
  - `tokens.css`
  - `faceplace_marketbook_icon.svg`

The launchers find Python, build `.venv`, install requirements, fetch Chromium
and run the app, stamping each step so repeat runs skip through. Everything from
the requirements check onwards is a loop, so an app that has just updated itself
can ask to be started again on the new files — see [Updating a copy that was
never cloned](#updating-a-copy-that-was-never-cloned). *Log into Facebook* is the
same launcher with `--login`, which shuts Chromium down cleanly and so writes the
new session to the profile.

### Files the app writes

Everything the app maintains for itself lives under `.state/` (git-ignored), at
the top of the project folder rather than inside `src/`. `paths.py` is the only
place these are named.

| Path | What it holds |
| --- | --- |
| `fb_session/` | Browser profile holding the login session. Must stay out of version control. |
| `marketplace_results.sqlite` | Cumulative archive across all runs. Listings are upserted, so re-seeing one updates its price, title, and `scraped_at`. |
| `my_locations.json` | Cities you added yourself, so a personal city is never a change to a tracked file. |
| `saved_searches.json` | Scheduled searches and how often each one runs. |
| `email_config.json` | SMTP address and app password. Must stay out of version control. |
| `shortcuts.json` | Which shortcuts exist, and whether the offer was waved away. |
| `update.json` | When the repository was last asked for its version, what it said, and any version waved away. |
| `update/` | Scratch space for an update in progress. Empty at rest. |
| `schedule/`, `debug/` | The run lock and tick log; `--debug-dump` output. |

Two things live outside it. `runs/` holds one folder per run, git-ignored but
deliberately *not* under `.state/`, because it's the one piece of working state
anyone opens on purpose. `~/Library/Application Support/FaceplaceMarketbook/`
holds the scheduler's log and last check-in, outside the project because macOS
can deny a background task access to it and the message saying so has to land
somewhere.

## Running it from a terminal

Python 3.9 or newer and one third-party package (`playwright`); Chromium is
downloaded by Playwright itself. 3.9 is a real floor — the launchers refuse
anything older, and the app is tested against it.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
.venv/bin/python src/fb_marketplace_sweep.py            # opens the settings window

.venv/bin/python src/fb_marketplace_sweep.py --query "land rover defender" \
    --exclude "can am, otterbox" --min-price 4000 --max-price 120000

.venv/bin/python src/fb_marketplace_sweep.py --query "defender 110" --query "land rover 90"
```

`--ui` forces the window even when flags are given, seeding the form with them;
`--no-ui` always goes straight to the sweep. Scheduled searches have their own entry
point, `src/scheduling.py`: `--tick` (what launchd and Task Scheduler call: every
due search in creation order, under one lock), `--list`, `--run NAME`,
`--install`, `--uninstall`, `--test-email`, `--no-email`, and `--verify-probe`.
So does the updater, `src/updater.py`: no arguments reports whether a newer
version exists, `--update` installs it. See [Updating a copy that was never
cloned](#updating-a-copy-that-was-never-cloned).

| Flag | Description |
| --- | --- |
| `--query TEXT` | Search query. Without it (and without `--no-ui`) the window opens. Repeat it for up to 5 OR'd queries. |
| `--exclude TERMS` | Comma-separated terms to reject, ignoring case, spaces, and punctuation. |
| `--min-price N` / `--max-price N` | Price bounds, applied server-side and locally. |
| `--min-year Y` / `--max-year Y` | Model-year bounds, read from the listing title. Local only. |
| `--exclude-no-year` | With a year bound set, also drop titles with no year in them. |
| `--only LABEL` | Run only locations whose label contains `LABEL`. |
| `--import-urls FILE` | Parse pasted search URLs into `locations.json`. |
| `--out CSV` | Explicit CSV path; skips the per-run `runs/<query>_<date>/` folder. |
| `--pace NAME` | `fast` (~7s per listing, default) or `slow` (~9s). |
| `--match TERM` / `--limit N` | Only describe listings whose title contains `TERM`, or only the first `N`. |
| `--scrolls N` | Max scrolls per city — a safety ceiling only. Default `60`. |
| `--exact` | Ask Facebook for tight matching (default is loose). |
| `--keep-all` | Keep non-matching listings, flagged in `source_section` / `matches_query`. |
| `--no-descriptions` / `--no-thumbs` / `--no-gallery` | Skip that stage. `--thumbnails-dir DIR` moves the thumbnail folder. |
| `--no-pause` / `--no-open` | Skip the post-login pause, or don't open the gallery. |
| `--login` | Refresh the saved Facebook session and exit. |
| `--set-radius` | Open Marketplace to check or restore the account search radius. |
| `--desktop-icon` | Put a double-clickable icon on the desktop and exit. |
| `--debug-dump` | Save raw Facebook JSON payloads for troubleshooting extraction. |
| `--descriptions CSV` / `--download-thumbs CSV` | One-off passes over an existing CSV instead of a sweep. |

## The app window

`run_from_ui()` is a loop, not a single pass, because the settings window is the
app as far as anyone using it is concerned. It opens the window, runs whatever the
window submitted, and opens the window again — so a finished search leaves you
somewhere you can start another one instead of at a dead terminal.

The order at the end of a run matters and is the reason `run()` isn't the one
opening the gallery any more. The window goes up first; the gallery is opened from
the `on_ready` callback, once the page is loaded, which puts the listings in front
and the app underneath them. `run_from_ui()` therefore passes `open_gallery=False`
and holds the path until the next opening. If that opening fails, a `finally`
opens the gallery anyway: a window that couldn't start is a reason to say where the
results went, not to swallow them.

Closing the window, or pressing Cancel, is how the app is quit — nothing is
running and nothing is half-finished at that point, so there is nothing left to
stay open for. It exits with `CLOSED_EXIT` (76), which the launchers read as
"nothing in this terminal is worth reading" and skip their *press Return to close
this window* on, so quitting the app doesn't leave a terminal behind to be
dismissed as well. (Whether the terminal window itself disappears is your
terminal's own setting — on macOS, Terminal ▸ Settings ▸ Profiles ▸ Shell ▸ *When
the shell exits*.) The other exit code the launchers watch for is 75, which asks
to be started again on a version the app has just installed over itself.

"Run now" on a scheduled search comes back to the window too, but with no gallery:
those results go out by email, and the run itself is `scheduling.tick(force=...)`,
which passes `open_gallery=False` for the same reason.

### Shortcuts

`make_desktop_icon.offer()` answers two separate questions, which is why it always
returns a `places` list and only sometimes an `ask` of true. `places` is everywhere
this machine can put a shortcut, all of them ticked, so **Add shortcut** on its own
does the lot; `ask` is whether to put the panel up unprompted, which happens on a
launch with no shortcut anywhere and no `never_ask` recorded. The Email & Setup tab
has a **Create a shortcut** button that opens the same panel from the list alone,
so saying "not now", or moving the folder and stranding an icon, doesn't leave the
command line as the only way back. Adding replaces whatever is already there,
which is exactly what fixing a moved folder needs.

## The four stages

1. **Sweep** — visit each location and scroll its results, filtering as it goes
   and stopping once three scrolls turn up nothing that could pass the filters
   (see [How deep it scrolls](#how-deep-it-scrolls)). A location is swept once
   per query (see [Several queries](#several-queries)).
2. **Retrieve descriptions** — visit each kept listing's detail page *once* for
   the description and full-size photo, writing each to the database the moment
   it's done.
3. **Thumbnails** — save every image locally, reusing anything already on disk.
4. **Gallery** — write `gallery.html` and `lightweight_gallery.html`, and open
   the first in a browser.

Ctrl-C at any point in the first three stops that stage where it stands and
jumps to the fourth with whatever has been found (see
[Interrupts](#interrupts-and-why-windows-needs-care)).

Results from every city are merged by listing id *before* the description and
thumbnail stages, so overlapping city radii never cause duplicate work.

## How extraction works

Most durable first. **Structured JSON**: GraphQL responses captured while
scrolling, plus embedded `<script type="application/json">` blobs in the initial
page. These carry typed fields (title, price, location, mileage, photo URI,
description) and survive markup churn far better than CSS classes, which are all
machine-generated. **DOM fallback**: result-card anchors matched by the
`/marketplace/item/<id>` href pattern, with line-classification heuristics for
price, location, mileage, and title. If a sweep suddenly returns empty fields,
run `--debug-dump` and inspect the saved payloads.

## Filtering

In order of how much junk they remove:

- **`--exclude`** is the big one when the search term is an overloaded brand name.
  A term's words have to appear in order, each at the start of a word, separated
  by at least one space or punctuation mark: `can am` catches "Can-Am", "CAN AM"
  and "Can-Ams", but not "canam", and `fender` catches "fender flares" without
  matching the "fender" inside "Defender". On a real "defender 110" sweep this
  alone cut 4,698 listings to 1,925, nearly all of it Can-Am UTVs.

  The separator is what stops a term from disappearing inside a longer word, and
  it costs a little coverage: of 1,736 cards in one Medford sweep, 9 were spelled
  "Canam" or "CanAm" and need that as its own term. The alternative — matching on
  the letters alone — is what made `fender` match all 267 Land Rovers in that
  same feed.
- **`--min-price` / `--max-price`** are sent to Facebook in the URL *and* applied
  locally. A price floor removes the parts that legitimately say "Defender 110":
  shocks, doors, a $75 window, a $1 transmission. Took that same sweep to 811.
- **`--min-year` / `--max-year`** read the model year out of the title, the same
  way the gallery does for its year sort: the first 4-digit number that could be
  a year, bounded to 1900..next year so a trim or part number can't pose as one.
  Sellers who omit it are kept unless `--exclude-no-year` says otherwise, and
  that switch only takes effect alongside a bound — on its own it would discard
  every listing whose seller just wrote "Land Rover Defender".
- **Query words** are all required. Every run of letters and digits in the query
  is a word — numbers and one- or two-letter words included, so "defender 110"
  keeps no 90s and "vw bus" keeps no unbranded buses — and each is matched at a
  word start, so "chev" catches both "Chevy" and "Chevrolet" while "van" won't
  match "advantage".
- **Numbers still rank as well as filter.** A number in the *title* earns more
  than the same number somewhere in the card text, so listings that lead with it
  are described first and an interrupted or capped description run spends its
  time on the best candidates.

**Why `--exact` is off by default.** Measured head-to-head on one city with
everything else identical, `exact=true` returned 63 raw cards versus 1,599, and
after filtering yielded 39 listings against 73 — every one of which was already
in the loose set. It found nothing new while discarding 34 genuine Defender 110s,
including a $30,995 2022 and an $86,992 2025. Fast reconnaissance, not better.

## Several queries

A search is a list of up to `MAX_QUERIES` (5) query strings: AND within a query,
OR between them. `query_groups()` turns them into one list of required words per
query, and `matches_query()` keeps a listing whose text satisfies any one group.
The exclude terms are unaffected — one excluded word drops a listing whichever
query found it — and so is everything after them, since the price and year
bounds belong to the search rather than to a query.

Within a city the sightings are merged by listing id before anything is filtered, so a listing
found by two queries is one row and costs one description. The two sightings can
disagree — each query gets its own feed, and a listing that sat past the
out-of-radius divider in one may sit well inside it in another — so
`better_row()` keeps the sighting with the better claim to being kept rather than
whichever arrived second. The scroll probe tests all the queries at once for the
same reason: a card that answers a different query than the one this pass is
scrolling for is still a real match, and will be kept.

`run.json` records the whole search as `query` (the queries joined with " OR ",
which is also what the CSV's `query` column carries) and as `queries`, the list.
The run folder is named for the **first query alone**: every one of them spelled
out runs into the length a path is allowed to be, and the whole search is written
down inside the folder either way. Per-city scroll counters are summed across the queries so
that everything downstream still reads one line per city, with the individual
queries kept under `per_city.<city>.per_query` when there was more than one.

A scheduled search stores `queries`, the list, and `query`, the same thing on one
line. `queries` is the authority: `normalize_search()` derives `query` from it on
every read and write, and fills the list in from `query` for a search saved
before queries existed. The report, the log lines and the saved-search card all
read the list, so a two-query search reads as 'defender 110' or 'land rover 90'
rather than as one odd-looking string.

## Search radius

The radius is an **account-level Marketplace setting**, not a URL parameter —
none of the obvious URL spellings affect it. It defaults to 250 miles, and the
saved cities are spaced assuming 500, so a fresh account quietly searches about a
quarter of the intended area, with no error to show for it. So
`preflight_pause()` runs between login and the first city: it reads the radius
out of the page's own `filter_radius_km` payload, prints it, and blocks on
`input()` while you fix it, then re-reads it so `run.json` records the value
actually used.

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
Each city reports where it stopped and why, and `run.json` records it under
`per_city`.

Two filters are held out of that in-loop test: the **exclude terms** and the
**year bounds**. Both narrow a feed Facebook is still ordering by its own
relevance, and a run of excluded or wrong-year cards says nothing about whether
the listings being looked for have run out — fold them in and three scrolls of
2004s, or of Can-Ams, would end a city while the ones asked for were still
further down. Price stays in, because Facebook applies price server-side as well,
so a priced-out listing largely never arrives to be counted.

Measured on one Medford sweep of "defender 110" — 1,736 cards over the full
60-scroll ceiling, with every card's probe result recorded at the scroll it
appeared on, so both rules could be replayed against the identical feed:

| exclude terms in the probe | stops at | listings recovered by removing it |
| --- | --- | --- |
| `can am` (25% of matching cards) | scroll 35 vs 37 | 0 |
| half of matching cards removed | scroll 34 vs 37 | ~0 |
| three quarters removed | scroll 28 vs 37 | ~6 of 67 |
| nine tenths removed | scroll 15 vs 37 | ~9 of 28 |

So it costs two scrolls in the ordinary case and buys nothing there; the reason
to hold exclude out is the bottom of that table, where a heavy exclude list would
otherwise end a city in the first handful of scrolls. Filtering by year or by
exclude terms therefore costs a little extra scrolling, and the results are the
reason to spend it.

Retrieving descriptions is the other slow stage, and most of *its* cost is a
deliberate randomized pause between detail-page hits — the one knob that trades
runtime against how machine-like the traffic looks. Neither pace can go below the
fixed ~5s per listing it takes to load a detail page and store its photo, which
is why "fast" saves less than the pause numbers suggest.

## Locations

Location handling uses search URLs grabbed by hand, so there's no fragile
city-picker automation. `parse_location()` takes a full search URL, a bare
`/marketplace/<seg>` URL, or just the segment — a slug and a numeric id both
work. `--import-urls FILE` does the same for a text file of pasted URLs.

**Two files, one list.** Which cities ship is decided in code, by
`BUILTIN_LOCATIONS`: that list is a coverage guarantee — the twelve spaced so
their 500-mile circles tile the continental US — and a file any hand-edit can
shorten is a poor place for one. `locations.json` is the tracked, read-only copy,
consulted for segments rather than membership, so a name it adds is ignored and a
name it loses comes back from code. `my_locations.json` is git-ignored and takes
every write, so adding a personal city is never a repo change and no edit can
delete a shipped one.

**A city Facebook doesn't recognise.** Validation stops at the shape of the
segment, because whether `fdjsklfjsdkl` is a city is Facebook's question to
answer — and Facebook's answer is the awkward part. An unrecognised segment
doesn't 404: it redirects to `/marketplace/category/search` and serves results
for whatever location the *account* is set to, a full page of real listings that
would be filed under the made-up city's label and look like coverage. Every real
city keeps its segment in the final URL, so the redirect is the signal;
`city_was_dropped()` checks for it after each navigation, the sweep skips that
city, and the omission becomes a warning at the top of the emailed report.

## The gallery

A normal run builds it; `python3 src/build_gallery.py runs/<folder>/results.csv`
rebuilds one from a CSV without re-scraping. It's a single portable file, with
thumbnails baked in as data URIs, so it needs no `thumbnails/` folder, no server,
and no live Facebook URLs. `--no-embed` links them by path instead and keeps the
file small (a few hundred KB rather than ~10 MB per 150 listings), but then it
only works from its original folder.

A run writes both: `gallery.html` embedded, and `lightweight_gallery.html` with
`--no-embed`, from the same CSV in the same folder. The `image` column already
holds `thumbnails/<item_id>.jpg` for anything downloaded, so the unembedded
build is the CSV's own paths passed straight through, and the two files stay in
step because they're generated together.

**Year sorting** reads the model year out of the title, since that's where
vehicle sellers put it and no listing field carries it. The first `19xx`/`20xx`
match bounded to 1900..next year wins, which keeps trim and spec numbers ("2500
lb winch") from reading as years, and listings with no year sort to the bottom in
*both* directions, since "no year" is missing data rather than an extreme value.
**Hiding** a card with its `✕` is remembered by listing id in local storage, so
it survives sorting, reloads, and even rebuilding the file from a later sweep.

### Getting back to one

`past_runs.py` is the Past searches tab: `list_runs()` walks `runs/` — the dated
folders directly under it, plus one level into `runs/saved/` — and turns each
into a card, and `open_run()` opens that run's gallery in the everyday browser
rather than in the Playwright window asking for it.

Nothing in that window may ever navigate it. It has no address bar to come back
from, its profile isn't logged into Facebook, and it closes the moment a search
starts — so a plain `<a href>` in the settings page would strand whoever clicked
it. `settings.js` catches clicks on any `http` link and hands the address to
`settings_ui.open_link()`, which is `webbrowser.open()`. That's how the "how to
get a city's link" instructions can link to Marketplace: the browser it opens in
is the one already logged in, which is the whole point of going there.

`run.json` is where the numbers come from, but a folder holding only a
`results.csv` still gets a card, with the listing count read off the CSV and the
time taken from the file's own timestamp. A run whose manifest never got written
is exactly the run someone is most likely to want back, and the alternative to
showing it is paying to do it again.

Two things a card's id has to survive. It's the folder's path relative to
`runs/`, and it makes the round trip through the page, so `folder_for()` resolves
it and refuses anything that doesn't land inside `runs/`. And a run that never
built a gallery — the stage turned off, or it failed — has one built on the spot
from its CSV, so every card is clickable rather than a third of them being dead.

`delete_run()` removes a whole run folder, which is why it goes through
`folder_for()` and then insists on `is_run()` as well: that second check is what
keeps `runs/saved` itself — a real directory inside `runs/`, but never a card —
from being handed to `rmtree`. The window asks for a second click before calling
it, the same as the scheduled searches list does, and that click is the only guard
there is. A scheduled search survives losing its folder, because what it remembers
about listings it has already seen is in the database, not in there.

## Interrupts, and why Windows needs care

Ctrl-C is a way of finishing a run, not a way of failing one. At any stage it
stops the work, keeps what has been gathered, and goes straight to the CSV and
the gallery — so an hour of sweeping is never thrown away for wanting the second
hour back. Four places catch it, each keeping the work at a different grain:

| Where | What is kept |
| --- | --- |
| `collect_city()`, mid-scroll | The cards already read off the page. Facebook recycles cards scrolled past, so they're snapshotted incrementally and there is always a complete set in hand. The city comes back with `stop_reason` `STOPPED_BY_HAND`. |
| The city loop in `run()` | The city being swept, as far as it got, plus every city before it. Cities are committed to SQLite as they finish, so the ones behind it were never at risk. |
| `retrieve_descriptions()` | Every listing described so far; each is committed as it finishes. Returns `False`. |
| The `fetch_thumbs()` call | Every photo already downloaded. |

Whichever fires, the rest of the stages are skipped — including the check on
listings that stopped appearing, since a check that didn't finish must not
confirm anything sold — and the run winds up through the same CSV → gallery →
`run.json` path an uninterrupted one takes. The manifest and the summary carry
`interrupted` and `interrupted_during`, which is what puts the warning at the top
of a scheduled search's report.

Two things are deliberately not salvaged. A Ctrl-C before the first city — at
the login screen or the preflight pause — exits instead, because there is
nothing to save; it also takes the run folder back with it, since the folder is
made before the browser opens and Past searches lists folders. And a stop that
finds nothing at all writes nothing at all, rather than leaving an empty run
behind.

### Closing the browser window

Closing the window a run is driving is the same ending, and is delivered as one.
`browser.WindowClosed` subclasses `KeyboardInterrupt`, so all four handlers above
catch it without having to know it exists. `stop_if_window_closed()` raises it,
and is called from the handlers that would otherwise shrug an error off and carry
on: the navigation retry, the card snapshot, the per-listing failure in
`retrieve_descriptions()`, the per-photo failure in `save_image()`. Those are
right to be forgiving about one page — but once the window is gone, every page
left fails the same way, and thousands of silent failures in a row is the one
response worse than stopping.

Two places poll instead, having no failing call to notice. The login wait, whose
`is_logged_in()` answers False for a window that isn't there and would otherwise
sit out its full ten minutes; and the preflight pause, which is blocked on
`input()` and checks `page.is_closed()` once that's answered.

What this replaces was worse than a lost run. The error came out of whichever
Playwright call was in flight and ended the process without unwinding, which left
the run lock on disk and the Chromium profile locked — so the *next* launch
couldn't start either, until the lock aged out after `LOCK_STALE_HOURS`. Ending as
an interrupt fixes both, because the interrupt path already lets go of everything
on its way to writing the CSV.

The window says so while it works: `launch_context()` adds an init script
(`NOTICE_JS`) drawing a fixed note along the bottom of every page — closing this
window ends the search, press Ctrl-C in the terminal instead. `pointer-events:
none`, because the login, the popups and the radius control all need clicking and
nothing this app draws over Facebook may ever be in the way. A real "are you
sure?" isn't available: Chromium's own leave-the-page prompt arrives as a
`beforeunload` dialog, which Playwright intercepts and answers itself, so nobody
closing the window would ever see it. `notice=False` for the two windows opened
for a person to use — `--login` and `--set-radius` — where closing the window when
you're done is how they're meant to end.

**After a Ctrl-C, nothing may talk to the browser again.** This is the rule the
whole thing hangs on, and it isn't obvious. Playwright's sync API runs on a
greenlet; an exception raised out of a blocked call leaves that machinery
wedged, and *the next call into it never returns* — no error, no timeout, just a
run stopped one step short of writing the hour of work it is holding. Measured,
not assumed: `browser_context.close()` after an interrupt was still blocked
ninety seconds later, whether the signal went to Python alone or to the whole
process group the way a terminal sends it.

So on the way out, every call that would reach the browser is skipped rather
than wrapped — `ctx.close()`, the `page.unroute()` in `retrieve_descriptions()`,
the `page.remove_listener()` after the sweep, the page's embedded
`<script type="application/json">` blobs. Nothing is lost by skipping them:
leaving the `sync_playwright()` block stops the driver, which takes the browser
with it, and that returns immediately even from the wedged state. Only work that
touches disk — the CSV, the galleries, `run.json` — happens after an interrupt.

Windows has its own version of this. A console sends `CTRL_C_EVENT` to *every*
process attached to it, and Playwright's driver is a separate `node.exe` sharing
that console, so the driver can die at the same instant Python raises the
exception; a teardown call then talks to a connection that no longer exists and
raises, where the same call on macOS hangs. Both are handled by not making the
call. Also worth knowing when reading a "it just froze" report: selecting text in
a Windows console suspends the process at its next write to stdout, which looks
identical to a hang. `Esc` releases it.

A long run has one more way to lose its browser connection, which is the machine
going to sleep. `keep_awake()` wraps each long-running stage and asks the OS to
stay up — `caffeinate -ims -w <pid>` on macOS, `SetThreadExecutionState` via
`ctypes` on Windows — though neither can defeat closing a laptop lid.

## Scheduled searches

A scheduled search is a settings dict plus an interval, stored in
`saved_searches.json`. `scheduling.py` is one module rather than several because
launchd and Task Scheduler need a single entry point to call, and the runner, the
schedule arithmetic and the report all have to agree about the same state files.

**Email is a prerequisite, enforced as one.** A scheduled search's entire output is a
message, so the `save_search` hook refuses outright while `email_ready()` is
false, and the window is told at render time — `email_config` carries a `ready`
flag — so the save block is shut before anything is typed into it rather than
after. Email setup used to be a separate tab someone could simply not visit, and
skipping it produced searches that ran on time, found things, and told nobody.

**Times are naive local wall clock.** A daily search means 5am the way a person
means it, on both sides of a daylight-saving change, and the whole system runs on
one laptop; UTC would make 5am drift by an hour twice a year. `next_run_at()`
measures from **`last_started`, not `last_finished`**, so a run that takes 40
minutes doesn't ratchet the schedule forward. Two guards keep the arithmetic
honest: a computed target is advanced until it's actually in the future, since a
run that fired late must not leave the next one in the past where it would fire
again immediately; and for hour-based intervals, fires that were slept through
are skipped rather than queued.

### Only positive evidence removes a listing

The interesting question each run is which previously-found listings are gone,
and absence from the feed is not evidence: Facebook's ranking is not a promise,
and a listing that's still live routinely fails to appear. So
`reconcile_with_previous()` carries every missing listing forward unless a check
positively confirmed it, and `classify_listing()` returns `unknown` for anything
ambiguous.

Checks are tiered. Tier 1 is an HTTP request through Playwright's request
context, which carries the session cookies without rendering: about a second,
against roughly seven for a full detail-page visit. Only what Tier 1 can't call
either way goes to Tier 2, a real page load with media blocked, and a listing
that appeared in this run's feed is never checked at all. A redirect to `/login`
or `/checkpoint` raises `SessionExpired`, because mistaking an expired session
for "every listing was deleted" would silently destroy the tracked set.

### Cost, output, and one run at a time

A scheduled run passes `describe_new_only=True`, so it only visits detail pages
for listings it has never described: the first run costs the same as a manual
one, and after that the cost is proportional to what's new. This depends on
`upsert()` not blanking what it already has — a sweep only sees search cards, so
its `description` is always empty and its `image` a remote URL that will expire,
and `KEEP_IF_BLANK` makes a blank incoming value lose to a stored one.

`saved_run_dir()` gives one stable folder per scheduled search, `runs/saved/<slug>/`,
rewritten in place with previous reports archived into `history/`; a scheduled
search that made a new dated folder every run would bury the results it exists to
surface. `run_lock` is an `O_CREAT | O_EXCL` file holding the pid and start time:
Chromium would refuse the shared browser profile anyway, and the lock turns that
crash into an explained skip. `tick()` holds it for a whole batch, where a
`SessionExpired` stops the batch — every remaining search would fail at the same
wall — and sends one email rather than one each.

### Email

Reports go out over SMTP with the user's own app password, carrying two HTML
galleries: this run's new listings, and everything tracked.
`build_attachments()` budgets them — each re-renders without embedded thumbnails
if it exceeds `ATTACH_MAX_MB` (12), and if the pair still won't fit in
`COMBINED_MAX_MB` (22), the full gallery gives up its thumbnails first, since the
complete version is already on disk and the new listings are the part worth
looking at on a phone. Only Gmail's host is special-cased by name;
`smtp_target()` falls back to a user-supplied host and port.

**The link to the real gallery is written to survive not working.**
`_gallery_html()` turns the run's path into a `file://` link through
`Path.as_uri()`, which encodes the spaces and ampersands a raw href would
mangle. That link is only meaningful on the machine that holds the file: read on
a phone it does nothing, read after the folder moved it lands on the browser's
own "file not found", and Gmail strips `file://` hrefs before rendering. So the
link text is never "click here" — the path stays printed underneath as plain
text, the sentence beside it names the attachments as the copy that opens
anywhere, and a path that can't become a URL at all (a relative one, which would
resolve against the reader's machine and point somewhere real and wrong) is left
as text with no link at all.

**Credentials fail at the wrong layer, so the shape is checked early.** A
mistyped address and a wrong password are the same error to a mail server: it
refuses the login and says the password was rejected, sending people to re-copy a
password that was always fine. So `address_problem()` refuses anything that
cannot be an address before it's written to disk, and `_smtp_hint()` names the
address alongside the password when authentication fails. Correctness beyond that
needs the server, which is what the test send is for.

### Waking the machine

macOS gets a LaunchAgent with `StartInterval` for the tick, plus
`pmset repeat wakeorpoweron` for a daily wake at 5am — launchd cannot wake a
sleeping Mac on its own. `pmset` needs root, so installing runs it through
osascript's administrator-privileges dialog; declining leaves the schedule
working, just waiting for the machine to be awake. That daily wake is the only
one that reliably exists, since `rearm_wake()`'s one-off wake for the next due
run runs unattended, where its `sudo -n` can't prompt for a password.

Windows gets a scheduled task with `WakeToRun`, `StartWhenAvailable`, and
`DisallowStartIfOnBatteries` false. One setting can't be automated: **Allow wake
timers** defaults to disabled on battery, so `install_schedule()` returns that as
a message for the UI to show.

### macOS hides Documents from launchd, silently

A LaunchAgent gets no TCC grant, so every file under `~/Documents`, `~/Desktop`
and `~/Downloads` is denied to it — `exists()` returns true and the read raises
`PermissionError`. A process started from Terminal is fine, because it inherits
Terminal's grant, so this cannot be reproduced by running the tick by hand. From
inside `~/Documents`, launchd starts the interpreter, reports exit status 0, and
nothing whatsoever happens: no run, no log line, no error. Three things follow,
all structural. The scheduler's log cannot live in the project folder, or the one
message explaining the refusal would itself be refused, so `SUPPORT_DIR` holds
it. The install has to prove the agent works rather than assume it: every
`--tick` writes `check_in()` to `SUPPORT_DIR` first, and `install_schedule()`
clears that file, bootstraps the agent, and waits for `RunAtLoad` to produce a
new one, so no check-in means the system stopped us and the install says so. And
silence has to stay readable afterwards, which is why `schedule_problems()`
reports a schedule that has never checked in, or whose last check-in is older
than three ticks, as *on, but blocked* rather than green.

Both fixes are offered because they suit different people: moving the folder out
of `~/Documents` needs no password, while granting Full Disk Access leaves the
folder where it is. The advice names the exact interpreter path, since a venv
Python is not something anyone will find in that list by browsing.

## Updating a copy that was never cloned

Almost nobody who runs this cloned it. They clicked Download ZIP, so there's no
git in the folder to pull with, and re-downloading would strand the login, the
scheduled searches, the database and any shortcut pointing at the old folder.
`updater.py` does the same job in place instead.

The version lives in `src/version.py` as `__version__`, and the check reads that
same file straight out of the repository over `raw.githubusercontent.com`.
Deliberately not a release or a tag: bumping one line and pushing it is then the
whole release process, and there's no second place to remember to update. The
cost is a CDN that holds the old copy for a few minutes after a push, so a
version bumped a moment ago isn't visible quite yet.

It asks on every launch rather than on a timer, and it asks before the window
opens. The notice most worth showing is the one about an update whose author has
just told someone to go and get it, and a cached answer can't carry that news —
so a rule like "at most once a day" is wrong exactly when it matters. The price
is that the check sits in front of the window: about a third of a second on a
working connection, and `CHECK_TIMEOUT` on a broken one, after which it says
nothing. That's a fair trade here, because a machine that can't reach GitHub in
two seconds is about to have a much harder time driving Facebook.

Asking every launch means asking *once* per launch. A run that prints the
terminal notice and then opens the window would otherwise pay for two lookups
and two timeouts, so the answer is memoised in the process for both to read —
including an answer of "don't know", which is why the sentinel for "not asked
yet" is its own object rather than `None`. `.state/update.json` keeps the last
answer across launches, which is what lets the banner stay up on a machine that
heard about an update yesterday and is offline today, along with any version that
was waved away.

`announce()` narrates the check in the terminal, and it reports even when there's
no news. It used to speak up only when an update was waiting, which made three
quite different situations look identical from the outside: up to date, a check
that failed silently, and a check that never ran. That's a bad trade for one line
of output, and it's most confusing for the one person guaranteed to hit it — the
author, watching for a version they just pushed and unable to tell CDN lag from a
bug. So `available()` returns a `why` alongside `show`, and each value of it gets
its own sentence:

| `why` | What the line says |
| --- | --- |
| `clone` | No check was made, because a clone updates with `git pull`. |
| `unreachable` | GitHub couldn't be reached and nothing was remembered, so this launch can't tell either way. |
| `ahead` | This copy is *newer* than the repository — pushed moments ago, or not pushed at all. |
| `current` | Up to date, and which version that is. |
| `skipped` | A newer version exists and this copy was told to stop asking about it. |
| `newer` | The offer, plus where the button is. |

The window reads `show` and ignores the rest. Any answer that came from
`.state/update.json` rather than from GitHub says so, because "up to date" on the
strength of yesterday's answer is a weaker claim than it sounds.

Installing downloads the branch zip from `codeload.github.com`, unpacks it to a
temp folder, and refuses to go on unless what came back has the files this
project has — a captive-portal login page and a truncated download both arrive
looking like a success otherwise. Only then is anything in the project folder
touched.

Four rules govern what happens next, and each exists for a reason worth keeping:

- **Nothing under `.state/`, `runs/`, `.venv/` or `.git/` is read from the zip or
  written to.** That's the whole reason updating in place beats re-downloading.
- **Every file is installed as a rename**, not a write over the top. The shell is
  part-way through reading the very launcher being replaced and holds the old
  file open, so a rename leaves the running script intact where a truncate-and-
  write would corrupt it mid-execution. A root-level file that won't budge —
  Windows holding a launcher open — is left on the old version and reported,
  rather than failing the whole update.
- **Files about to be overwritten are copied aside first and put back if
  anything raises.** Downloading and unpacking are free to fail; this part isn't,
  so a full disk or a closed lid leaves the folder as it was rather than half of
  each version. The copy is deleted on success and nobody is ever told it
  existed — a backup a user has to restore by hand isn't a recovery plan.
- **`src/` and `docs/` are pruned to match the zip**, so a module deleted upstream
  doesn't linger where something might still import it. Nothing outside those two
  folders is ever deleted, because the project root is where people keep things.

The update also takes the same `run_lock` a sweep takes, and holds it throughout.
A run that started an hour ago has most of the program loaded but not all of it —
`build_gallery` is imported at the very end of `run()`, and `scheduling.py` does
the same — so replacing the files underneath one would have a single sweep finish
on a mixture of two versions, and pruning could delete a module it hasn't reached
yet. The lock closes that from both directions: an update won't start while a
sweep is going, and a scheduled sweep that comes due mid-update finds the lock
held and skips, which is already what it does when a manual run is in the way.
The lock is per project folder, so a second copy unpacked elsewhere is a separate
install and unaffected.

Installing doesn't put the running process on the new version. Python read the
old modules into memory minutes ago and has no reason to read them again, so the
app has to be started afresh — and it can't do that for itself, because it would
have to survive its own replacement to reinstall the libraries a new version
might need. Only the launcher is in a position to do it properly.

So the launcher offers, and the app takes it up. The launcher sets
`FACEPLACE_RELAUNCH` to an exit code it watches for, runs the app inside a loop,
and on seeing that code goes back round through the dependency install and
starts Python again. The app reads the variable through `relaunch_code()`: set
means a restart can be promised, unset means the only honest thing to say is
"start it again yourself". Unset is what an old launcher from before this
existed looks like, and also a hand-typed `python src/fb_marketplace_sweep.py`
and the scheduler, none of which are watching an exit code. Putting the number
in the variable rather than agreeing one in advance keeps the two scripts from
drifting apart on what it is.

The two launchers get there differently, both times because of how the shell
reads a file it may be part-way through when that file gets replaced. Bash has
the whole loop in memory and holds the old inode open, so a `while` loop is
safe. `cmd` tracks its place in a batch file by byte offset, so jumping to a
label in one that changed underneath it would run whatever now sits at that
position; the Windows launcher runs `"%~f0"` instead, which reads the new file
from the top and never returns.

Nothing anywhere branches on *how far* behind a copy is. Eleven versions back and
one version back take the same path and read identically, because the difference
isn't actionable — there's one thing to do either way.

A folder with a `.git` in it is opted out entirely — that's a clone whose owner
has git, and unpacking a zip over their working tree would throw away whatever
they hadn't committed. `python src/updater.py` reports what's available and
`--update` installs it, both refusing to run in a clone for the same reason.

Once an update lands, the code on disk is newer than the code already running,
and a lazily imported module would come off disk as the new version. So the
window blocks itself behind a sheet whose only button closes it, rather than
letting someone start a sweep on half of each version.

## Testing

`tests/test_scheduling.py` covers the interval arithmetic including
daylight-saving and late-run cases, the classifier, the reconciliation rule, the
attachment budgets, message construction through a recording `smtplib.SMTP`, and
the lock — including a real second process, since single-process mutual exclusion
would prove nothing about the case that matters. `run_saved_search()` takes an
injectable `sweep`, so the whole pipeline is exercised without a browser.
`tests/test_settings_ui.py` opens the actual settings window in headless Chromium
and clicks through it with the real hooks; it's the only level that catches a
mis-wired button or a window close racing a submit. One thing it structurally
cannot see is the window itself: headless Chromium has no address bar, no tab
strip and no title bar, so whether the `--app` flag took — and with it, whether
the keyboard lands in the page or in an omnibox — only shows up in a real
launch. `tests/test_past_runs.py`
builds run folders in a temporary directory — with a manifest, without one, and
with a `history/` — and replaces `webbrowser.open`, so nothing opens a window on
whoever is running the suite. `tests/test_updater.py`
builds a throwaway project folder and a zip on the spot rather than touching the
network, and the test that earns its keep is the one that kills an update part
way through and asserts the folder is byte-for-byte what it was.

Three things need a live check. **The unknown-city rule**, because the behaviour
belongs to Facebook: a made-up segment should be skipped with the substitute
named, and each of the twelve should keep its segment in the final URL.
**That the OS actually runs a search unattended**: copy the project outside
`~/Documents`, arm a search with a `minutes` interval (`FACEPLACE_DEV=1` adds
that unit) and a `next_run` in the past, install, then touch nothing — the run
should appear in `tick.log`. And **the report's times**, since the sweep records
UTC and the report shows local, so an offset dropped instead of converted turns a
late-evening run into one that started tomorrow morning — arithmetic no assertion
about a timestamp catches.

## Gatekeeper

Every Mac user hits this once, and it isn't the usual quarantine problem — a
`git clone`'d copy with no `com.apple.quarantine` attribute is blocked just the
same, because the launcher is simply unsigned. macOS 15 removed the
Control-click → Open bypass, so the only routes are System Settings → Privacy &
Security → **Open Anyway**, or running the script from Terminal. Removing that
friction entirely would need a paid Apple Developer account and notarization.

## Cross-platform notes

- All paths are derived from the script's own location via `pathlib`, so the app
  doesn't care about the working directory.
- Both launchers set `PYTHONUTF8=1`, and every file read and write names its
  encoding explicitly, so accented listing titles behave the same regardless of a
  machine's regional settings.
- `.gitattributes` pins `*.bat` to CRLF and `*.command` to LF. A batch file with
  Unix line endings can misbehave on Windows, and the `.command` file must keep
  its executable bit to stay double-clickable.
