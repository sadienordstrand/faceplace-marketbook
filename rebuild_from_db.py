#!/usr/bin/env python3
"""Rebuild a CSV and gallery from the database, without touching Facebook.

Every listing is written to marketplace_results.sqlite as its city finishes, so
a run that dies partway through has already saved its sweep. This turns that
back into the outputs the run never got to write.

    python3 rebuild_from_db.py                     # list what's in there
    python3 rebuild_from_db.py --query "defender 110"
    python3 rebuild_from_db.py --query "defender 110" --since 2026-08-05

Descriptions and photos are included for whichever listings got that far.
"""
import argparse
import sqlite3
from pathlib import Path

from fb_marketplace_sweep import (DB_PATH, FIELDS, LEGACY_THUMBS_DIRNAMES,
                                  THUMBS_DIRNAME, write_csv, make_run_dir,
                                  relevance, query_tokens, query_numbers)


def connect():
    if not DB_PATH.exists():
        raise SystemExit(f"No database at {DB_PATH}. Nothing to rebuild from.")
    # Read-only: this is a recovery tool and must not be able to make things
    # worse than they already are.
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def summarize(con):
    rows = con.execute(
        "SELECT query, COUNT(*) n, SUM(description <> '') described,"
        "       MIN(scraped_at) first, MAX(scraped_at) last"
        "  FROM listings GROUP BY query ORDER BY last DESC").fetchall()
    if not rows:
        raise SystemExit("The database is empty.")
    print(f"{DB_PATH}:\n")
    for r in rows:
        print(f"  {r['n']:>6} listings  ({r['described'] or 0} with descriptions)"
              f"  query {r['query']!r}")
        print(f"         {r['first'][:16]} .. {r['last'][:16]}\n")
    print("Rebuild one with:  python3 rebuild_from_db.py --query \"<query>\"")


def fetch(con, query, since=None):
    sql = "SELECT * FROM listings WHERE query = ?"
    args = [query]
    if since:
        sql += " AND scraped_at >= ?"
        args.append(since)
    rows = [dict(r) for r in con.execute(sql, args)]
    if not rows:
        raise SystemExit(f"No listings for query {query!r}"
                         + (f" since {since}." if since else "."))
    # Same best-first order a normal run uses. It matters most here: the usual
    # next step is feeding this CSV back through --descriptions, which reads it
    # in order, so anything capped or interrupted should hit the good ones.
    tokens, numbers = query_tokens(query), query_numbers(query)
    rows.sort(key=lambda r: (-relevance(r, tokens, numbers), r.get("title", "")))
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--query", help="which search to rebuild (omit to list them)")
    ap.add_argument("--since", metavar="DATE",
                    help="only listings seen at or after this date, e.g. 2026-08-05")
    ap.add_argument("--out", metavar="CSV", help="where to write (default: a new runs/ folder)")
    ap.add_argument("--no-gallery", action="store_true", help="write the CSV only")
    a = ap.parse_args()

    con = connect()
    if not a.query:
        summarize(con)
        return

    rows = fetch(con, a.query, a.since)
    described = sum(1 for r in rows if r.get("description"))
    # Thumbnails are stored as paths relative to the run folder they were saved
    # in, so a rebuild that lands somewhere else can't resolve them. The gallery
    # falls back to the remote URL, which may have expired — say so rather than
    # let it look like a bug.
    local_imgs = sum(1 for r in rows
                     if (r.get("image") or "").startswith(LEGACY_THUMBS_DIRNAMES))

    out = Path(a.out) if a.out else make_run_dir(f"{a.query} rebuilt") / "results.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    write_csv(rows, out, FIELDS)
    print(f"\nRecovered {len(rows)} listings ({described} with descriptions).")
    print(f"Wrote {out}")

    if not a.no_gallery:
        import build_gallery
        # embed=False: the saved photos live in the original run folder, not
        # next to this CSV, so there is nothing local to embed.
        path = build_gallery.build(out, embed=False)
        print(f"Wrote {path}")
        if local_imgs:
            print(f"\n{local_imgs} listings point at photos saved in their original\n"
                  f"run folder. To see them, copy that folder's {THUMBS_DIRNAME}/ next\n"
                  f"to the file above.")


if __name__ == "__main__":
    main()
