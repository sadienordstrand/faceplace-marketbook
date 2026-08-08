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


def query_tokens(query):
    """Alphabetic words (3+ chars) from the query. Required to match."""
    return [t for t in re.findall(r"[a-z]+", query.lower()) if len(t) >= 3]


def query_numbers(query):
    """Numeric parts like '110'. Highly discriminating when present (761 of
    4,698 'defender' hits had it) but sellers often omit them, so these rank
    listings rather than filter them."""
    return re.findall(r"\d+", query)


def word_hits(token, hay):
    """Match at a word start, so 'defender' also catches 'Defenders' while 'van'
    doesn't match 'advantage'."""
    return re.search(r"\b" + re.escape(token), hay) is not None


def matches_query(tokens, *texts):
    hay = " ".join(t for t in texts if t).lower()
    return all(word_hits(t, hay) for t in tokens)


def squash(s):
    """Strip everything but alphanumerics so one --exclude term covers the
    'Can-Am' / 'Can Am' / 'CANAM' spellings that all appear in real listings."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def is_excluded(r, terms):
    if not terms:
        return False
    hay = squash(f"{r.get('title', '')} {r.get('raw_text', '')}")
    return any(squash(t) in hay for t in terms if t.strip())


def price_number(price):
    """Dollar amount as an int, or None when there's no usable price."""
    m = re.search(r"[\d,]+", (price or "").replace(" ", ""))
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return None


def relevance(r, tokens, numbers):
    """Ranks how likely a listing is the thing you actually searched for, so
    description retrieval spends its time at the top of the list."""
    title = (r.get("title") or "").lower()
    score = 0
    for n in numbers:
        if n in title:
            score += 3
    if tokens and all(word_hits(t, title) for t in tokens):
        score += 2  # every query word in the title, not just the card text
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


def build_rows(cards, divider_seen, json_listings, label, query, tokens):
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
            tokens, r["title"], r["raw_text"]) else "no"
    return rows


def keep_row(r, exclude=(), min_price=None, max_price=None):
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
    return True, ""


def card_may_keep(card, tokens, exclude=(), min_price=None, max_price=None):
    """The in-loop version of keep_row, used only to decide whether a scroll
    was worth doing.

    It sees a raw card before the structured-JSON merge, so it deliberately
    errs toward yes: a card whose text has not rendered, or whose price did not
    parse, counts as a match rather than risk cutting the scroll short. Real
    filtering still happens on the merged rows afterwards, so a generous answer
    here costs a couple of extra scrolls at worst."""
    if card.get("outside"):
        return False
    title, price, _loc, _miles, lines = parse_card_text(card.get("text", ""))
    raw = " | ".join(lines)[:300]
    if not title and not raw:
        return True
    r = {"source_section": "unknown", "title": title, "price": price,
         "raw_text": raw,
         "matches_query": "yes" if matches_query(tokens, title, raw) else "no"}
    ok, _why = keep_row(r, exclude, min_price, max_price)
    return ok
