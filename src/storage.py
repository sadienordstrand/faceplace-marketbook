"""
storage.py

Where results go: the per-run CSV, the cumulative SQLite database, the run
folders, and the rule for folding a previous run's results into a new one.

FIELDS is exactly the CSV's columns and exactly the `listings` table's columns.
The tables only scheduled searches need are kept in their own schema so that
stays true.
"""
import csv
import re
import sqlite3
from datetime import datetime

import paths

DB_PATH = paths.DB_PATH
RUNS_DIR = paths.RUNS_DIR

FIELDS = ["item_id", "title", "price", "url", "image", "listing_location", "miles",
          "description", "source_section", "matches_query", "location_searched",
          "query", "scraped_at", "raw_text"]


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
            # Order matters. A local path beats a
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


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_") or "search"


def make_run_dir(query, base=None):
    """runs/<query-slug>_<mm-dd-yyyy>, suffixed _1, _2, ... so a run can never
    overwrite an earlier one."""
    parent = (base or RUNS_DIR)
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
    d = (base or (RUNS_DIR / "saved")) / slugify(name)
    d.mkdir(parents=True, exist_ok=True)
    return d
