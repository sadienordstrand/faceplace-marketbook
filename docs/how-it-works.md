# How it works

Internals, command-line usage, and the measurements behind the advice in the
[README](../README.md). Nothing here is needed to use the app.

## Layout

- `fb_marketplace_sweep.py` — sweep, description retrieval, thumbnails, and the
  pipeline that runs them in order.
- `settings_ui.py` — the pre-flight settings window. It's HTML rendered in a
  Playwright window, so there's nothing extra to install and it matches the
  gallery's styling.
- `build_gallery.py` — turns a results CSV into a browsable `gallery.html`.
- `scheduling.py` — saved searches: persistence, interval arithmetic, the
  live/sold/gone classifier, email, the report, the run lock, and the OS-level
  schedule. Also the entry point launchd and Task Scheduler call.
- `tests/` — `test_scheduling.py` is offline and fast; `test_settings_ui.py`
  drives the real settings window in headless Chromium.
- `locations.json` — `label -> search segment` for the twelve shipped cities.
  Tracked, and only ever read.
- `my_locations.json` — cities you added yourself (git-ignored). Every add and
  remove writes here, so a personal city is never a change to a tracked file.
- `saved_searches.json` — saved searches and their schedules (git-ignored).
- `email_config.json` — SMTP address and app password (git-ignored, and it must
  stay that way: an app password is full access to that mailbox).
- `.schedule/` — the run lock and tick log (git-ignored).
- `~/Library/Application Support/FaceplaceMarketbook/` (`%LOCALAPPDATA%` on
  Windows) — the scheduler's own log and its last check-in. Outside the project
  because macOS can deny a background task access to the project folder, and the
  message saying so has to land somewhere.
- `Start Faceplace (Mac).command` / `(Windows).bat` — double-click launchers.
  They find Python, build `.venv`, install `requirements.txt`, fetch Chromium,
  and run the app. All work is stamped so repeat runs skip straight through.
- `Log into Facebook (Mac).command` / `(Windows).bat` — the same launcher with
  `--login`. Renewing an expired session otherwise meant starting a sweep and
  abandoning it, which risks killing Chromium before it writes the new session.
- `tests/make_screenshots.py` — regenerates `docs/images/` for the README from a
  staged set of saved searches and a real gallery. Never touches live config.
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

Saved searches have their own entry point:

```bash
.venv/bin/python scheduling.py --tick            # run whatever is due
.venv/bin/python scheduling.py --list            # saved searches and next run times
.venv/bin/python scheduling.py --run NAME        # run one now, ignoring its schedule
.venv/bin/python scheduling.py --install         # let the OS wake and call us
.venv/bin/python scheduling.py --uninstall
.venv/bin/python scheduling.py --test-email
.venv/bin/python scheduling.py --verify-probe URL...
```

| Option | Meaning |
| --- | --- |
| `--tick` | What launchd / Task Scheduler calls. Runs every due search in creation order under one lock. |
| `--run NAME` | Ignores the schedule for one search. Accepts its id or its name, case-insensitively. |
| `--no-email` | Run and write results, but send nothing. |
| `--verify-probe URL...` | Classify listing URLs as live / sold / gone and print the marker that decided each. |

### Locations

Location handling uses search URLs grabbed by hand, so there's no fragile
city-picker automation. Paste them into a text file, one per line, optionally
prefixed with a label and a comma:

```
Dallas, TX, https://www.facebook.com/marketplace/dallas/search/?query=...
https://www.facebook.com/marketplace/108173265878171/search/?query=...
```

Then `--import-urls locations_urls.txt` parses them into `my_locations.json`. A
slug like `dallas` and a numeric id both work. Note that `--import-urls`
*replaces* your own cities, and can't touch the shipped ones.

The settings window can append instead, which is the path most people use.
`parse_location()` takes whatever was pasted — a full search URL, a bare
`/marketplace/<seg>` URL, or just the segment — and pulls out the segment,
rejecting the feature URLs people grab by mistake (`/marketplace/item/...`,
`/marketplace/you/...`, `/marketplace/category/...`) rather than silently saving
a "city" that returns nothing. `add_location()` also refuses duplicate labels
and duplicate segments, naming the existing entry so the collision is obvious.
Adds and removes are written immediately, not at Start, so they survive
cancelling the window.

#### Two files, one list

The shipped cities and the user's own live apart:

- **Which cities ship is decided in code**, by `BUILTIN_LOCATIONS`. That list is a
  coverage guarantee — the twelve are spaced so their 500-mile circles tile the
  continental US — and a file that any hand-edit can shorten is a poor place to
  keep a guarantee.
- **`locations.json`** is the readable copy of that same list, tracked in git and
  *only ever read*. It can correct a segment Facebook has changed, a name it loses
  comes back from code, and a name it adds counts as one of yours.
- **`my_locations.json`** is git-ignored and takes every write.

The split exists because the single-file version wrote personal cities into a
tracked file, so adding one showed up as a repo change and a mis-edit could delete
a shipped city for good. `remove_location()` refuses the shipped ones and the
settings window renders no ✕ for them at all, since unticking a city achieves
everything a person actually wants from removing one and isn't destructive.

`migrate_own_locations()` moves anything left in `locations.json` that isn't
shipped into `my_locations.json`, once, so upgrading doesn't quietly drop a city
somebody added under the old layout. It's gated on `my_locations.json` *existing*
rather than on it having anything in it: an empty file means "I removed them all",
and re-reading the old file at that point would hand back the city just deleted —
which is what the first version of this did, caught by a test rather than by
anybody noticing.

#### A city Facebook doesn't recognise

Validation stops at the shape of the segment, because whether `fdjsklfjsdkl` is a
city is Facebook's question to answer, not something a regex can know. Facebook's
answer is the awkward part: an unrecognised segment doesn't 404. It redirects to
`/marketplace/category/search` and serves results for whatever location the
*account* is currently set to — a full page of real listings, which would be
stored under the made-up city's label and look like coverage.

Every real city, slug or numeric id, keeps its segment in the final URL, so the
redirect is the signal. `city_was_dropped()` checks for it after each navigation
and the sweep skips that city rather than filing another city's listings under it.
`city_shown()` reads `buy_location.display_name` out of the page so the message
can name the place Facebook substituted. Skipped cities are collected in the
summary's `unknown_cities`, which `run_saved_search` turns into a warning at the
top of the emailed report — silence there would mean a region going unsearched
every run with nothing to show it.

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
| `--descriptions-budget MIN` | Ask before retrieving descriptions would take longer than `MIN` minutes. Default `0`, which never asks. |
| `--yes` | Don't ask about long description jobs. |
| `--no-pause` | Skip the post-login pause for popups and the radius. |
| `--no-open` | Don't open the finished gallery in a browser. |
| `--set-radius` | Open Marketplace to check or restore the account search radius. |
| `--login` | Refresh the saved Facebook session and exit, without sweeping anything. What the "Log into Facebook" launcher runs. |
| `--match TERM` | Only describe listings whose title contains `TERM`. |
| `--limit N` | Only retrieve descriptions for the first `N` listings. |
| `--thumbnails-dir DIR` | Thumbnail folder (default: `thumbnails`). `--thumbs-dir` still works. |
| `--pace NAME` | Pause between detail-page hits while retrieving descriptions: `fast` (1-2.5s, ~7s per listing, default) or `slow` (3-5s, ~9s). |
| `--scrolls N` | Max scrolls per city — a safety ceiling only; the sweep normally stops much sooner, after 3 scrolls with no new matches. Default `60`. |
| `--exact` | Ask Facebook for tight matching (default is loose). |
| `--keep-all` | Keep filler/non-matching listings instead of dropping them (flagged in `source_section` / `matches_query`). |
| `--no-descriptions` / `--no-thumbs` / `--no-gallery` | Skip that stage. |
| `--debug-dump` | Save raw Facebook JSON payloads to `debug/` for troubleshooting extraction. |
| `--descriptions CSV` | One-off: retrieve descriptions for an existing CSV instead of running a sweep. |
| `--download-thumbs CSV` | One-off: download the image URLs in an existing CSV. |

The stage formerly called "enrich" is now "retrieve descriptions" throughout.
`--enrich`, `--no-enrich`, and `--enrich-budget` still work as undocumented
aliases so older notes and scripts don't break.

## The four stages

1. **Sweep** — visit each saved location and scroll its results, filtering as it
   goes and stopping once three scrolls in a row turn up nothing that could pass
   the filters (see [How deep it scrolls](#how-deep-it-scrolls)).
2. **Retrieve descriptions** — visit each kept listing's detail page *once* for
   the seller's description and full-size photo. Each listing is written to the
   database the moment it's done, and Ctrl-C stops the work without discarding
   it: the run skips the remaining downloads and goes straight to the CSV and
   gallery.
3. **Thumbnails** — save every image locally. The description stage stores each
   photo the moment it reads the URL, so nothing expires mid-run; anything
   already on disk is reused.
4. **Gallery** — write `gallery.html` and open it in a browser.

Each detail page is visited at most once per run and each image downloaded at
most once: results from every city are merged by listing id *before* the
description and thumbnail stages, so overlapping city radii never cause
duplicate work.

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
  a "110" isn't required — but listings that have it are described first, along
  with those whose titles contain every query word. An interrupted or capped
  description run therefore spends its time on the best candidates.

### Why `--exact` is off by default

Measured head-to-head on one city with everything else identical. `exact=true`
returned 63 raw cards versus 1,599, but after filtering yielded only 39 listings
against 73 — and every one of its 39 was already in the loose set. It found
nothing new while discarding 34 genuine Defender 110s, including a $30,995 2022
and an $86,992 2025. It's a fast reconnaissance mode, not a better one.

## Search radius

The radius is an **account-level Marketplace setting**, not a URL parameter —
none of the obvious URL spellings affect it. It defaults to 250 miles, and the
saved cities are spaced assuming 500, so a fresh account quietly searches about
a quarter of the intended area. This failure is invisible: no error, just fewer
results.

So `preflight_pause()` runs between login and the first city. It loads the first
search URL, reads the radius out of the page's own `filter_radius_km` payload,
prints it, and blocks on `input()` while you fix the radius and dismiss any
popups. It re-reads the radius afterwards, so the value recorded in `run.json`
is the one actually used. `--no-pause` skips it (the radius is still read and
printed), `EOFError` is caught so a non-interactive run continues rather than
crashing, and Ctrl-C at the prompt exits cleanly instead of tracebacking.

This is the same problem `--set-radius` was for; that flag still exists, but the
pause covers the case that actually bit people, which was not knowing there was
a radius to check.

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

Retrieving descriptions is the slow stage, and most of its cost is a deliberate
randomized pause between detail-page hits — the one knob that trades runtime
against how machine-like the traffic looks. `--pace fast` (default) waits
1-2.5s, landing near 7s per listing; `--pace slow` waits 3-5s for about 9s.

Neither can go below the fixed per-listing cost: ~3.5s to load a detail page and
read its payload, plus ~1.5s to fetch and store the photo when thumbnails are
on. That floor is why "fast" saves less than the pause numbers suggest.

The estimate is always printed before the stage starts.
`--descriptions-budget MIN` makes anything longer than that stop and offer the
top N by relevance, all of them, or none. It's `0` by default, which never
asks.

## The gallery

A normal run builds it. To rebuild from a CSV without re-scraping:

```bash
python3 build_gallery.py runs/<folder>/results.csv
```

It's one portable file: thumbnails are baked in as data URIs, so it needs no
`thumbnails/` folder, no server, and no live Facebook URLs. `--no-embed` links
thumbnails by path instead and keeps the file small (a few hundred KB rather
than ~10 MB per 150 listings), but then it only works from its original folder
with `thumbnails/` beside it, opened directly in a browser rather than a preview
pane.

Features: client-side text search, a searched-city filter, sorting by price,
title, or year, and a click-through detail view with the full description.

Year sorting reads the model year out of the title, since that's where vehicle
sellers put it and no listing field carries it. The first `19xx`/`20xx` match
bounded to 1900..next year wins, which keeps trim and spec numbers ("2500 lb
winch", "3500 miles") from reading as years. Listings with no year sort to the
bottom in *both* directions rather than clumping at one end, since "no year" is
missing data, not an extreme value; ties fall back to original order.

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

## Interrupts, and why Windows made them worse

Ctrl-C during the description stage is supposed to stop the work and keep it:
`retrieve_descriptions()` catches `KeyboardInterrupt`, returns `False`, and the
run skips the remaining downloads and goes straight to the CSV and gallery. Each
listing is already committed to SQLite as it finishes, so nothing is waiting in
memory.

On Windows that unravelled. A console sends `CTRL_C_EVENT` to *every* process
attached to it, and Playwright's driver is a separate `node.exe` sharing that
console — so the driver dies at the same instant Python raises the exception.
The `finally` block then called `page.unroute()` on a connection that no longer
existed, and that exception propagated out in place of the clean return, past
`finished = ...`, out of `run()`, and the CSV and gallery were never written.
The user saw "Everything gathered so far is saved" printed immediately before
the run died.

So every teardown call that talks to the browser after an interrupt is now
wrapped: `page.unroute()` and `ctx.close()`. The route handler installed for the
stage swallows its own failures too, since a request still in flight when the
page navigates away can't be answered and shouldn't be able to surface as a
stalled page. None of these can fail usefully — the browser is being discarded
either way.

Worth knowing when reading a "it just froze" report on Windows: selecting text
in a console window suspends the process at its next write to stdout, which
looks identical to a hang, browser included, because the process driving the
browser is stopped. `Esc` releases it. `main()` also forces line buffering on
stdout so a working run can't be mistaken for a stalled one.

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

## Saved searches

A saved search is a settings dict plus an interval, stored in
`saved_searches.json`. `scheduling.py` is one module rather than several because
launchd and Task Scheduler need a single entry point to call, and the runner, the
schedule arithmetic and the report all have to agree about the same state files.

### Times are naive local wall clock

Every timestamp in `saved_searches.json` is a naive local `datetime`. A daily
search means 5am the way a person means it, on both sides of a daylight-saving
change, and the whole system runs on one laptop. UTC would make 5am drift by an
hour twice a year.

`next_run_at()` measures from **`last_started`, not `last_finished`**, so a run
that takes 40 minutes doesn't push the following run 40 minutes later and ratchet
the schedule forward over time. Two guards keep the arithmetic honest: a computed
target is advanced until it's actually in the future (a run that fired late must
not leave the next one in the past, where it would fire again immediately), and
for hour-based intervals, fires that were slept through are skipped rather than
queued up — waking from a five-hour sleep runs the search once, not five times.

### Only positive evidence removes a listing

The interesting question each run is which previously-found listings are gone.
Absence from the feed is not evidence: Facebook's ranking is not a promise, and a
listing that's still live routinely fails to appear. So `reconcile_with_previous()`
carries every missing listing forward unless a check positively confirmed it, and
`classify_listing()` is written to return `unknown` for anything ambiguous.

Checks are tiered. Tier 1 is an HTTP request through Playwright's request context,
which carries the session cookies without rendering: about a second, against
roughly seven for a full detail-page visit. Only what Tier 1 can't call either way
goes to Tier 2, a real page load with images and media blocked. A listing that
appeared in this run's feed is never checked at all — appearing already proves it
exists, and that's free.

Two failure modes get explicit handling. A redirect to `/login`, `/checkpoint` or
`two_step_verification` returns `auth`, which aborts the whole check and raises
`SessionExpired` — mistaking an expired session for "every listing was deleted"
would silently destroy the tracked set. A timeout, a rate limit, or an
unrecognisable page returns `unknown`, which increments `verify_failures` and
leaves `status` alone. `needs_verifying()` skips anything checked within the last
interval, so a listing that's been missing for weeks isn't re-probed every run.

### Cost, and why later runs are cheap

Descriptions are the expensive stage, and a scheduled run passes
`describe_new_only=True`, so it only visits detail pages for listings it has never
described. The first run of a saved search costs the same as a manual one; after
that the cost is proportional to what's new, which is usually a handful.

This depends on `upsert()` not blanking what it already has. A sweep only sees
search cards, so its `description` is always empty and its `image` is always a
remote URL that will expire. `KEEP_IF_BLANK` and the `image` CASE in `UPSERT_SQL`
make a blank incoming value lose to a stored one, and a local thumbnail path beat
a fresh remote URL.

### Output folder

`saved_run_dir()` gives one stable folder per saved search,
`runs/saved/<slug>/`, rewritten in place. A scheduled search that made a new dated
folder every run would bury the results it exists to surface. `archive_previous()`
moves the outgoing `run.json` and reports into `history/` as `run-N.json`, so the
folder itself only ever holds current results.

### One run at a time

`run_lock` is an `O_CREAT | O_EXCL` file in `.schedule/` holding the pid and start
time. Chromium would refuse the shared `.fb_session` profile anyway; the lock
turns that crash into an explained skip, and it covers the manual-run case too,
since `run_from_ui()` takes the same lock. A lock is reclaimed if its pid is dead
or it's older than `LOCK_STALE_HOURS` (8), which is longer than any real run.

`tick()` holds the lock for a whole batch and runs due searches in creation
order. A `SessionExpired` from one search stops the batch — every remaining search
would fail at the same wall — and sends one email instead of one per search. Any
other exception is contained to that search.

### Email

Reports go out over SMTP with the user's own app password, as `multipart/mixed`
carrying a plain-text and an HTML alternative plus two HTML galleries: this run's
new listings, and everything tracked. `build_attachments()` budgets them: each
file re-renders without embedded thumbnails if it exceeds `ATTACH_MAX_MB` (12),
and if the pair still won't fit in `COMBINED_MAX_MB` (22) once base64 has inflated
them by a third, the full gallery gives up its thumbnails first — the complete
version is already on disk, and the new listings are the part worth looking at on
a phone.

Gmail dedupes on `Message-ID`, and it has already filed its own copy of a
self-addressed message under Sent, so the delivered copy can be dropped before it
reaches the inbox. `--test-email` detects that over IMAP and sets
`imap_append_inbox`, after which `send_email()` appends each report to the inbox
directly.

Only Gmail's SMTP and IMAP hosts are special-cased by name; `smtp_target()` falls
back to a user-supplied host and port, so any provider works and a Gmail address
is not required.

#### Credentials fail at the wrong layer, so the shape is checked early

A mistyped address and a wrong password are the same error to a mail server: it
refuses the login and says the password was rejected. Left alone, that sends
people to re-copy a password that was always fine. So `address_problem()` refuses
anything that cannot be an address before it is written to disk, `email_remarks()`
says so when the address doesn't match the chosen provider or the password isn't
the sixteen lowercase letters Google issues, and `_smtp_hint()` names the address
alongside the password when authentication fails.

Correctness beyond that needs the server, which is what the test send is for. The
failure it prevents is specific and silent: a run at 5am that completes, writes
every result, and then cannot send the one message that would tell you about it.
Nothing is lost — the gallery and the database are on disk, and the next run
carries on — but the only channel for reporting a broken email channel is email.

### Waking the machine

macOS gets a LaunchAgent with `StartInterval` for the tick, plus
`pmset repeat wakeorpoweron` for a daily wake and a `pmset schedule wake` re-armed
after every tick for the next due run — launchd cannot wake a sleeping Mac on its
own. The `pmset` calls need administrator rights, which is why installing asks for
a password; without them the schedule still works, it just waits for the machine
to be awake.

Windows gets a scheduled task with `WakeToRun`, `StartWhenAvailable`, and
`DisallowStartIfOnBatteries` false. One setting can't be automated: **Allow wake
timers** defaults to disabled on battery, and a sleeping laptop won't wake without
it. `install_schedule()` returns that as a message for the UI to show.

### macOS hides Documents from launchd, silently

A LaunchAgent gets no TCC grant, so every file under `~/Documents`, `~/Desktop`
and `~/Downloads` is denied to it — `exists()` returns true and the read raises
`PermissionError`. A process started from Terminal is fine, because it inherits
Terminal's grant, so this cannot be reproduced by running the tick by hand. From
inside `~/Documents` the observed behaviour is: launchd starts the interpreter,
reports exit status 0, and nothing whatsoever happens. Not a failed run — no run,
no log line, no error. `~/FaceplaceMarketbook` is unaffected; only those three
folder names are guarded.

Three things follow, all of them structural:

- **The scheduler's log cannot live in the project folder.** If it did, the one
  message explaining the refusal would itself be refused. `SUPPORT_DIR`
  (`~/Library/Application Support/FaceplaceMarketbook`) holds the agent's
  stdout/stderr and its check-in file, and is always writable wherever the
  project sits.
- **The install has to prove the agent works rather than assume it.** Every
  `--tick` writes `check_in()` to `SUPPORT_DIR` before touching anything else, so
  it lands even when the project folder is unreachable. `install_schedule()`
  clears that file, bootstraps the agent, and waits for `RunAtLoad` to produce a
  new one. No check-in means the OS started us and the system stopped us, and the
  install returns `(False, permission_help())` instead of a success message.
- **Silence has to be readable afterwards too.** `schedule_problems()` reports an
  installed schedule that has never checked in, or one whose last check-in is
  older than three ticks, and the settings window shows the schedule as *on, but
  blocked* rather than green. The same function catches a moved folder by
  comparing the installed plist against the current path, which is a separate way
  to end up with a schedule that runs nothing.

Both fixes are offered because they suit different people: moving the folder out
of `~/Documents` needs no password and no system settings, while granting Full
Disk Access to `python_exe()` leaves the folder where it is. The advice names the
exact interpreter path, since a venv Python is not something anyone will find in
that list by browsing.

### Testing

`tests/test_scheduling.py` covers the interval arithmetic including
daylight-saving and late-run cases, the classifier against fixtures for every
status, the reconciliation rule, the report and attachment budgets, message
construction through a recording `smtplib.SMTP`, and the lock — including a real
second process, since single-process mutual exclusion would prove nothing about
the case that matters. `run_saved_search()` takes an injectable `sweep`, so the
whole pipeline — folder reuse, archiving, bookkeeping, report, attachments,
email, failure paths — is exercised without a browser.

`tests/test_settings_ui.py` opens the actual settings window in headless Chromium
with the real hooks and clicks through it, which is the only way to catch a
mis-wired button or an exposed function that never answers. It found three bugs
that Python-level tests could not: a `display: flex` rule beating the browser's
own `[hidden]` rule, a window close racing a submit and overwriting the result,
and an unguarded response field.

`--verify-probe URL...` classifies real listing URLs and prints the marker that
decided each one, which is how the classifier's rules get checked against what
Facebook actually serves. Setting `FACEPLACE_DEV=1` adds a `minutes` interval unit
to the settings window, so a two-run cycle can be observed without waiting an
hour.

The unknown-city rule came out of a live probe and needs one to re-verify, because
the whole behaviour belongs to Facebook. Point a sweep at a made-up segment and it
should skip the city and name the substitute; point it at each of the twelve — the
numeric ids especially — and every one should keep its segment in the final URL. A
change to that redirect on Facebook's side would either start skipping real cities
or stop catching fake ones, and both are quiet failures.

Two more things only a live check can establish, both worth repeating after
changes to the scheduling code. The first is that the OS actually runs a search
unattended:
copy the project to a folder outside `~/Documents`, arm a search with a `minutes`
interval and a `next_run` in the past, install, and then touch nothing — the run
should appear in `tick.log` and the check-in should end with `"event":
"finished"`. The second is the report itself: reading a rendered report caught a
run at 11:12pm being described as "started tomorrow at 5:12 am", because the
sweep records UTC and `parse_iso` was discarding the offset rather than
converting it.

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
