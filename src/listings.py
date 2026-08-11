"""
listings.py

Turning a Marketplace page into rows: reading Facebook's own JSON, classifying
the lines of a result card, and deciding which listings survive the filters.

Structured JSON comes first. Facebook ships the data it renders from — GraphQL
responses while scrolling, and <script type="application/json"> blobs in the
initial page — and those typed fields survive markup churn far better than the
machine-generated CSS classes. Card text is the fallback, and it establishes
page order, which is how the out-of-radius divider is found.
"""
import json
import re
from datetime import datetime, timezone
from functools import lru_cache

ITEM_RE = re.compile(r"/marketplace/item/(\d+)")
PRICE_LINE_RE = re.compile(r"\$[\d,]+(?:\.\d{2})?")
LOC_RE = re.compile(r".+,\s*[A-Z]{2}")
MILES_RE = re.compile(r"[\d.,]+\s*[Kk]?\s*miles", re.I)
BADGE_RE = re.compile(
    r"just listed|pending|sponsored|in stock|out of stock|popular|"
    r"ships to you|see more like this|new listing", re.I)

# Reads the text of every <script type="application/json"> the page carries.
SCRIPT_JSON_JS = "els => els.map(e => e.textContent || \'\')"


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


# How many query strings a search may OR together. A ceiling rather than a
# limit anyone should reach: each one is a full scroll of every selected city, so
# five queries across twelve cities is 60 sweeps.
MAX_QUERIES = 5


def query_list(queries):
    """The queries of a search, as a list, blanks dropped.

    Takes either one string or a list of them, because a search carries several
    OR'd queries and nearly every caller has only ever had the one."""
    if isinstance(queries, str) or queries is None:
        queries = [queries or ""]
    return [q.strip() for q in queries if isinstance(q, str) and q.strip()]


def query_label(queries):
    """The whole search as one line, for folder names, logs and the CSV."""
    return " OR ".join(query_list(queries))


def query_tokens(query):
    """The words of one query, every one of which a listing has to contain."""
    return re.findall(r"[a-z0-9]+", (query or "").lower())


def query_groups(queries):
    """One list of words per query. A listing has to contain every word of at
    least one of them — AND within a query, OR between them."""
    return [t for t in (query_tokens(q) for q in query_list(queries)) if t]


def query_numbers(queries):
    """Numeric parts like '110'. Every query word is required now, so these no
    longer decide what to keep; they rank what was kept, because a number in the
    title is a stronger signal than the same number somewhere in the card."""
    return list(dict.fromkeys(re.findall(r"\d+", query_label(queries))))


def word_hits(token, hay):
    """Match at a word start, so 'defender' also catches 'Defenders' and 'chev'
    catches both 'Chevy' and 'Chevrolet', while 'van' doesn't match
    'advantage'."""
    return re.search(r"\b" + re.escape(token), hay) is not None


def matches_query(groups, *texts):
    """Whether the texts satisfy any one of the query word groups.

    No words to require — an empty query, or one that was all punctuation —
    matches everything, which is what having asked for nothing means."""
    if not groups:
        return True
    hay = " ".join(t for t in texts if t).lower()
    return any(all(word_hits(t, hay) for t in group) for group in groups)


@lru_cache(maxsize=256)
def exclude_pattern(term):
    """One --exclude term as a regex, or None if there's nothing to match on.

    The term's words have to appear in that order, each starting a word, with
    at least one space or punctuation mark between them. So 'can am' covers
    'Can-Am' and 'CAN AM' — the spellings of the same words — but not 'canam',
    which is a different word, and 'fender' no longer sits inside 'Defender'
    and takes every Land Rover in the sweep with it.

    Word start rather than whole word, matching how query words are handled, so
    'can am' still catches 'Can-Ams' without anyone having to think about it."""
    parts = re.findall(r"[a-z0-9]+", (term or "").lower())
    if not parts:
        return None
    return re.compile(r"\b" + r"[^a-z0-9]+".join(re.escape(p) for p in parts))


def is_excluded(r, terms):
    if not terms:
        return False
    hay = f"{r.get('title', '')} {r.get('raw_text', '')}".lower()
    pats = (exclude_pattern(t) for t in terms)
    return any(p.search(hay) for p in pats if p)


def price_number(price):
    """Dollar amount as an int, or None when there's no usable price."""
    m = re.search(r"[\d,]+", (price or "").replace(" ", ""))
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return None


YEAR_RE = re.compile(r"\b(1[89]\d{2}|20\d{2})\b")
EARLIEST_YEAR = 1900


def latest_year():
    """One year ahead, because next year's models are listed before it arrives."""
    return datetime.now(timezone.utc).year + 1


def year_number(title):
    """The model year in a title, or None when there isn't a plausible one.

    Deliberately the same rule the gallery uses for its year sort (yearOf in
    ui/gallery.html): vehicle sellers almost always lead with the model year, so
    it's the first 4-digit number in the title that could be one, bounded so trim
    numbers and part numbers can't masquerade as a year. The two have to agree —
    otherwise a listing could pass a year filter here and then sort as undated
    there, in the gallery that same run produced."""
    latest = latest_year()
    for m in YEAR_RE.finditer(str(title or "")):
        y = int(m.group(1))
        if EARLIEST_YEAR <= y <= latest:
            return y
    return None


def relevance(r, groups, numbers):
    """Ranks how likely a listing is the thing you actually searched for, so
    description retrieval spends its time at the top of the list."""
    title = (r.get("title") or "").lower()
    score = 0
    for n in numbers:
        if n in title:
            score += 3
    if groups and matches_query(groups, title):
        score += 2  # a whole query in the title, not just in the card text
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


def build_rows(cards, divider_seen, json_listings, label, query, groups):
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
            groups, r["title"], r["raw_text"]) else "no"
    return rows


def better_row(a, b):
    """The same listing seen under two of a search's queries, reduced to one row.

    They can disagree: each query gets its own feed, so a listing that sat past
    the out-of-radius divider in one may sit well inside it in another, and a
    card that hadn't finished rendering in one has its title in the other. Since
    both sightings are the same listing, take the one with the better claim to
    being kept rather than whichever happened to come second."""
    def rank(r):
        return (r.get("source_section") != "outside_search",
                r.get("matches_query") == "yes",
                len(r.get("title") or ""), len(r.get("raw_text") or ""))
    return b if rank(b) > rank(a) else a


def keep_row(r, exclude=(), min_price=None, max_price=None,
             min_year=None, max_year=None, include_no_year=True):
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
    # include_no_year only means anything alongside a bound. On its own it would
    # throw away every listing whose seller didn't put a year in the title, which
    # is not what unchecking a box next to an empty year range asks for.
    if min_year is not None or max_year is not None:
        y = year_number(r.get("title"))
        if y is None:
            if not include_no_year:
                return False, "no year in title"
        elif min_year is not None and y < min_year:
            return False, "under min year"
        elif max_year is not None and y > max_year:
            return False, "over max year"
    return True, ""


def card_may_keep(card, groups, min_price=None, max_price=None):
    """The in-loop version of keep_row, used only to decide whether a scroll
    was worth doing.

    It sees a raw card before the structured-JSON merge, so it deliberately
    errs toward yes: a card whose text has not rendered, or whose price did not
    parse, counts as a match rather than risk cutting the scroll short. Real
    filtering still happens on the merged rows afterwards, so a generous answer
    here costs a couple of extra scrolls at worst.

    A card matching any of the search's queries carries the scroll on, even
    though this city is being scrolled for one particular query: it is a real
    match for the search, and it will be kept when the rows are filtered.

    Only the query words and the price bounds are tested. The exclude terms and
    the year bounds are left out permanently, because both of them narrow a feed
    Facebook is still ordering by its own relevance: a stretch of excluded or
    wrong-year listings says nothing about whether the ones being looked for have
    run out, and three of those in a row would end the city while they were still
    further down the page. Price stays because Facebook applies it server-side as
    well, so a priced-out listing largely never arrives to be counted.

    Measured on one Medford sweep of "defender 110" (1,736 cards, 60 scrolls):
    holding the exclude terms out moved the stop from scroll 35 to 37. It costs
    two scrolls in the ordinary case, and it stops a heavy exclude list from
    ending a city in the first handful."""
    if card.get("outside"):
        return False
    title, price, _loc, _miles, lines = parse_card_text(card.get("text", ""))
    raw = " | ".join(lines)[:300]
    if not title and not raw:
        return True
    r = {"source_section": "unknown", "title": title, "price": price,
         "raw_text": raw,
         "matches_query": "yes" if matches_query(groups, title, raw) else "no"}
    ok, _why = keep_row(r, (), min_price, max_price)
    return ok
