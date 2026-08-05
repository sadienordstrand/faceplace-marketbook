#!/usr/bin/env python3
"""
settings_ui.py
--------------
The pre-flight settings window for fb_marketplace_sweep.py.

Rendered as a small HTML page in a Chromium window driven by Playwright, which
is already a dependency, so there's nothing extra to install and the styling can
share the gallery's palette and typewriter faces exactly. The page calls back
into Python through an exposed function, so no local web server is involved.
"""
import json

from playwright.sync_api import sync_playwright

FONTS = ("https://fonts.googleapis.com/css2?family=Lato:ital,wght@0,400;0,700;"
         "1,400&family=Courier+Prime:wght@400;700&display=swap")

CSS = """
  :root {
    --olive: #373D1F; --olive-dk: #262B14; --olive-lt: #5C6537;
    --accent: #ED926B; --accent-soft: #F0AC8E;
    --bg: #16180F; --card: #212418; --card-hi: #2A2E20;
    --ink: #EDE3CE; --ink-soft: #A79E85; --rule: #3A3F2B;
    --body: "Lato", -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif;
    --type: "Courier Prime", "American Typewriter", "Courier New", monospace;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font-family: var(--body); -webkit-font-smoothing: antialiased;
  }
  header {
    background: var(--olive); border-bottom: 5px solid var(--accent);
    padding: 18px 26px; position: sticky; top: 0; z-index: 5;
  }
  .brand {
    font-family: var(--type); font-weight: 700; font-size: 15pt;
    letter-spacing: .3em; text-transform: uppercase;
  }
  .sub {
    font-family: var(--type); font-size: 10pt; letter-spacing: .22em;
    text-transform: uppercase; color: var(--accent-soft); margin-top: 6px;
  }
  main { padding: 22px 26px 130px; max-width: 980px; margin: 0 auto; }
  section {
    background: var(--card); border: 1px solid var(--rule); border-radius: 3px;
    padding: 16px 18px; margin-bottom: 16px;
  }
  h2 {
    margin: 0 0 14px; font-family: var(--type); font-size: 11pt;
    letter-spacing: .24em; text-transform: uppercase; color: var(--accent);
    font-weight: 700;
  }
  label.field { display: block; margin-bottom: 16px; }
  label.field:last-child { margin-bottom: 0; }
  .lab {
    display: block; font-family: var(--type); font-size: 10pt;
    letter-spacing: .16em; text-transform: uppercase; color: var(--ink-soft);
    margin-bottom: 7px;
  }
  input[type=text], input[type=number] {
    width: 100%; padding: 10px 12px; color: var(--ink);
    background: var(--olive-dk); border: 1px solid var(--rule);
    border-radius: 2px; font-family: var(--type); font-size: 11pt;
    caret-color: var(--accent);
  }
  input:focus { outline: none; border-color: var(--accent); }
  input::placeholder { color: #6E6852; }
  .row { display: flex; gap: 16px; }
  .row > * { flex: 1; }
  .stack > label.field { margin-bottom: 18px; }
  .hint {
    font-size: 11pt; color: var(--ink-soft); margin-top: 8px; line-height: 1.55;
  }
  .hint ul { margin: 8px 0 0; padding-left: 20px; }
  .hint li { margin-bottom: 6px; }
  .hint li:last-child { margin-bottom: 0; }
  code { font-family: var(--type); font-size: 10.5pt; color: var(--accent-soft); }

  /* Toggles and segmented controls share the typewriter button look. */
  .toggles { display: flex; flex-wrap: wrap; gap: 9px; }
  .tog {
    display: inline-flex; align-items: center; gap: 10px; cursor: pointer;
    padding: 9px 13px; background: var(--olive-dk);
    border: 1px solid var(--rule); border-radius: 2px;
    font-family: var(--type); font-size: 10pt; letter-spacing: .1em;
    text-transform: uppercase; color: var(--ink-soft); user-select: none;
  }
  .tog:hover { border-color: var(--olive-lt); color: var(--ink); }
  .box {
    width: 16px; height: 16px; border: 1px solid var(--ink-soft); flex: none;
    border-radius: 1px; display: grid; place-items: center; font-size: 10pt;
    line-height: 1; color: transparent;
  }
  .tog[aria-pressed="true"] { color: var(--ink); border-color: var(--accent); }
  .tog[aria-pressed="true"] .box {
    background: var(--accent); border-color: var(--accent); color: #241608;
  }
  .seg { display: inline-flex; border: 1px solid var(--rule); border-radius: 2px; overflow: hidden; }
  .seg button {
    background: var(--olive-dk); color: var(--ink-soft); border: 0; cursor: pointer;
    padding: 10px 18px; font-family: var(--type); font-size: 10pt;
    letter-spacing: .12em; text-transform: uppercase;
    border-right: 1px solid var(--rule);
  }
  .seg button:last-child { border-right: 0; }
  .seg button[aria-pressed="true"] { background: var(--accent); color: #241608; font-weight: 700; }
  .seg button:hover:not([aria-pressed="true"]) { background: var(--card-hi); color: var(--ink); }

  .cities { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
  .mini {
    background: none; border: 1px solid rgba(237,146,107,.45); color: var(--accent);
    border-radius: 2px; padding: 5px 11px; cursor: pointer;
    font: 10pt var(--type); letter-spacing: .14em; text-transform: uppercase;
  }
  .mini:hover { background: var(--accent); color: #241608; }
  .seclab { display: flex; align-items: center; justify-content: space-between; }
  .seclab .grp { display: flex; gap: 7px; margin-bottom: 14px; }
  #hiddenWhenNoEnrich[hidden] { display: none; }

  footer {
    position: fixed; bottom: 0; left: 0; right: 0; background: var(--olive);
    border-top: 5px solid var(--accent); padding: 15px 26px;
    display: flex; align-items: center; justify-content: space-between; gap: 18px;
  }
  .est {
    font-family: var(--type); font-size: 10pt; letter-spacing: .1em;
    text-transform: uppercase; color: var(--ink);
  }
  .est b { color: var(--accent); }
  .actions { display: flex; gap: 10px; }
  .go, .cancel {
    font-family: var(--type); text-transform: uppercase; cursor: pointer;
    border-radius: 2px; letter-spacing: .18em; font-size: 11pt; padding: 12px 26px;
  }
  .go { background: var(--accent); color: #241608; border: 1px solid var(--accent); font-weight: 700; }
  .go:hover { background: var(--accent-soft); }
  .go:disabled { opacity: .45; cursor: not-allowed; }
  .cancel { background: none; color: var(--ink-soft); border: 1px solid var(--rule); }
  .cancel:hover { color: var(--ink); border-color: var(--ink-soft); }
  .warn { color: var(--accent); font-family: var(--type); font-size: 10pt; }
"""

HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Faceplace Marketbook — Search Setup</title>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="__FONTS__" rel="stylesheet">
<style>__CSS__</style>
</head><body>
<header>
  <div class="brand">Faceplace Marketbook</div>
  <div class="sub">Search setup</div>
</header>
<main>
  <section>
    <h2>Search</h2>
    <label class="field">
      <span class="lab">Query</span>
      <input type="text" id="query" placeholder="defender 110" autofocus>
      <div class="hint">
        Facebook decides what to return, and it isn't strict keyword matching —
        it treats your words as a guideline and mixes in related inventory, so most of
        what comes back won't contain your search term at all. This script then
        filters that down.
        <ul>
          <li>The script will drop listings whose titles don't contain
            <i>every</i> 3+ letter word in your query, so more words means narrower results.</li>
          <li>Words match at their start, so "defender" also catches
            "Defenders".</li>
          <li>The script ignores 1-2 letter words in your query ("of",
            "VW").</li>
          <li>The script also ignores any numbers in your query (like "110" or
            "1980") when deciding what to drop, but it still helps to include
            numbers, because it helps the script know which listings to
            prioritize.</li>
        </ul>
      </div>
    </label>
    <div class="toggles">
      <div class="tog" id="exact" role="button" tabindex="0" aria-pressed="false">
        <span class="box">✓</span>Exact matching
      </div>
    </div>
    <div class="hint">
      <b>Exact matching</b> asks Facebook to be much stricter about matching your query, but often ends up excluding real results. It's useful for if you want fast results, but in general, it's better to leave it off.
    </div>
  </section>

  <section>
    <div class="seclab"><h2>Cities</h2>
      <div class="grp">
        <button class="mini" id="allCities">All</button>
        <button class="mini" id="noCities">None</button>
      </div>
    </div>
    <div class="hint" style="margin: 0 0 14px">The search will include a 500 mile
      radius around each selected city. Select all cities to get search results
      from the entire continental US.</div>
    <div class="cities" id="cities"></div>
  </section>

  <section>
    <h2>Quality filters</h2>
    <div class="row">
      <label class="field"><span class="lab">Min price ($)</span>
        <input type="number" id="min_price" placeholder="any" min="0"></label>
      <label class="field"><span class="lab">Max price ($)</span>
        <input type="number" id="max_price" placeholder="any" min="0"></label>
    </div>
    <label class="field"><span class="lab">Exclude terms</span>
      <input type="text" id="exclude" placeholder="rhd, can am, hot wheels">
      <div class="hint">Comma separated. Punctuation and spaces are ignored when
        matching, so "can am" also catches "Can-Am" and "CANAM".</div>
    </label>
  </section>

  <section>
    <h2>Stages</h2>
    <div class="toggles">
      <div class="tog" id="do_enrich" role="button" tabindex="0" aria-pressed="true">
        <span class="box">✓</span>Retrieve Descriptions
      </div>
      <div class="tog" id="do_thumbs" role="button" tabindex="0" aria-pressed="true">
        <span class="box">✓</span>Retrieve Thumbnails
      </div>
      <div class="tog" id="do_gallery" role="button" tabindex="0" aria-pressed="true">
        <span class="box">✓</span>Build gallery
      </div>
      <div class="tog" id="debug_dump" role="button" tabindex="0" aria-pressed="false">
        <span class="box">✓</span>Dump raw payloads
      </div>
    </div>
    <div class="hint"><b>Dump raw payloads</b> saves the raw JSON Facebook sent
      into the run folder's <code>debug/</code>. Only useful for troubleshooting. For example,
      if titles or prices start coming back empty, that means Facebook changed the shape of
      its data, and the dump is what you'd inspect to fix it. It's bulky —
      roughly 8 MB per city — so leave it off normally.</div>
  </section>

  <section id="enrichBlock">
    <h2>Description Retrieval</h2>
    <div class="lab">Retrieval pace</div>
    <div class="seg" id="pace"></div>
    <div class="hint">Select "Slow" if you're worried about Facebook flagging
      your account for bot activity.</div>
    <div class="stack" style="margin-top:18px">
      <label class="field"><span class="lab">Limit (blank = all)</span>
        <input type="number" id="limit" placeholder="all" min="1">
        <div class="hint">This caps how many listings get a description, highest
          relevance first, so a limit of 100 fetches descriptions for the 100 most promising
          matches and leaves the rest with just their card details.</div>
      </label> 
      <label class="field"><span class="lab">Ask above (minutes, blank = never)</span>
        <input type="number" id="enrich_budget" value="__BUDGET__"
               placeholder="never ask" min="0">
        <div class="hint">If retrieving descriptions for all listings would take
          longer than this many minutes, the run pauses in the terminal and
          offers you three choices: retrieve descriptions for as many as it can
          in the allotted time, do all of them anyway, or skip description
          retrieval. Leave it blank to never ask.</div>
      </label>
    </div>
  </section>
</main>
<footer>
  <div class="est" id="est"></div>
  <div class="actions">
    <button class="cancel" id="cancel">Cancel</button>
    <button class="go" id="start">Start sweep</button>
  </div>
</footer>
<script>
const LOCATIONS = __LOCATIONS__;
const PACES = __PACES__;
const DEFAULTS = __DEFAULTS__;
// Fixed per-listing costs no pace setting can remove: loading the page and
// reading its payload, plus saving the photo when thumbnails are on.
const PAGE_WORK = DEFAULTS.page_work || 3.5;
const PHOTO_SAVE = DEFAULTS.photo_save || 1.5;

const $ = id => document.getElementById(id);
const secsPer = (p, withThumbs) =>
  PAGE_WORK + (withThumbs ? PHOTO_SAVE : 0) + (PACES[p][0] + PACES[p][1]) / 2;

// Cities
const cityWrap = $('cities');
LOCATIONS.forEach((label, i) => {
  const d = document.createElement('div');
  d.className = 'tog'; d.dataset.city = label; d.setAttribute('role', 'button');
  d.tabIndex = 0; d.setAttribute('aria-pressed', 'true');
  d.innerHTML = `<span class="box">✓</span>${label}`;
  cityWrap.appendChild(d);
});
$('allCities').onclick = () => {
  cityWrap.querySelectorAll('.tog').forEach(t => t.setAttribute('aria-pressed', 'true'));
  refresh();
};
$('noCities').onclick = () => {
  cityWrap.querySelectorAll('.tog').forEach(t => t.setAttribute('aria-pressed', 'false'));
  refresh();
};

// Pace segmented control. Labels carry the per-listing cost, which shifts when
// thumbnails are toggled, so they're filled in by refresh().
let pace = DEFAULTS.pace || 'fast';
const seg = $('pace');
Object.keys(PACES).forEach(p => {
  const b = document.createElement('button');
  b.dataset.pace = p;
  b.onclick = () => { pace = p; refresh(); };
  seg.appendChild(b);
});

// Any .tog element toggles on click or Enter/Space.
document.addEventListener('click', e => {
  const t = e.target.closest('.tog');
  if (!t) return;
  t.setAttribute('aria-pressed', t.getAttribute('aria-pressed') === 'true' ? 'false' : 'true');
  refresh();
});
document.addEventListener('keydown', e => {
  if (e.key !== 'Enter' && e.key !== ' ') return;
  const t = e.target.closest('.tog');
  if (t) { e.preventDefault(); t.click(); }
});

const on = id => $(id).getAttribute('aria-pressed') === 'true';
const num = id => $(id).value === '' ? null : Number($(id).value);

function collect() {
  return {
    query: $('query').value.trim(),
    cities: [...cityWrap.querySelectorAll('.tog')]
      .filter(t => t.getAttribute('aria-pressed') === 'true')
      .map(t => t.dataset.city),
    exact: on('exact'),
    min_price: num('min_price'),
    max_price: num('max_price'),
    exclude: $('exclude').value.trim(),
    do_enrich: on('do_enrich'),
    do_thumbs: on('do_thumbs'),
    do_gallery: on('do_gallery'),
    debug_dump: on('debug_dump'),
    pace: pace,
    limit: num('limit'),
    enrich_budget: num('enrich_budget'),
  };
}

function refresh() {
  const c = collect();
  seg.querySelectorAll('button').forEach(b => {
    b.setAttribute('aria-pressed', b.dataset.pace === pace ? 'true' : 'false');
    const s = Math.round(secsPer(b.dataset.pace, c.do_thumbs));
    b.textContent = `${b.dataset.pace} (${s}s per listing)`;
  });
  // The whole block is meaningless with description retrieval off.
  $('enrichBlock').hidden = !c.do_enrich;
  const parts = [`<b>${c.cities.length}</b> ${c.cities.length === 1 ? 'city' : 'cities'}`];
  if (c.do_enrich) {
    parts.push(`<b>${Math.round(secsPer(pace, c.do_thumbs))}s</b> per listing`);
    parts.push(c.limit ? `<b>${c.limit}</b> max` : `<b>all</b> listings`);
  } else parts.push('no descriptions');
  $('est').innerHTML = parts.join(' &middot; ')
    + (c.query ? '' : ' &middot; <span class="warn">query required</span>');
  $('start').disabled = !c.query || c.cities.length === 0;
}

$('query').addEventListener('input', refresh);
['min_price','max_price','exclude','limit','enrich_budget']
  .forEach(id => $(id).addEventListener('input', refresh));

$('start').onclick = () => { $('start').disabled = true; window.pySubmit(collect()); };
$('cancel').onclick = () => window.pyCancel();
document.addEventListener('keydown', e => {
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && !$('start').disabled) $('start').click();
  if (e.key === 'Escape') window.pyCancel();
});

if (DEFAULTS.query) $('query').value = DEFAULTS.query;
if (DEFAULTS.exclude) $('exclude').value = DEFAULTS.exclude;
refresh();
$('query').focus();
</script>
</body></html>
"""


def render(locations, paces, defaults):
    defaults = dict(defaults or {})
    # 0 / None means "never ask", which the form shows as an empty field.
    budget = defaults.get("enrich_budget") or ""
    return (HTML
            .replace("__FONTS__", FONTS)
            .replace("__CSS__", CSS)
            .replace("__BUDGET__", str(budget))
            .replace("__LOCATIONS__", json.dumps(list(locations)))
            .replace("__PACES__", json.dumps(paces))
            .replace("__DEFAULTS__", json.dumps(defaults)))


def collect_settings(locations, paces, defaults=None, headless=False):
    """Opens the settings window and blocks until Start or Cancel.

    Returns the settings dict, or None if cancelled or the window was closed.
    """
    html = render(locations, paces, defaults)
    state = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless,
                                    args=["--window-size=1000,1120"])
        page = browser.new_page(**({} if not headless
                                   else {"viewport": {"width": 1000, "height": 1120}}),
                                no_viewport=not headless)
        page.expose_function("pySubmit", lambda data: state.update(done=True, data=data))
        page.expose_function("pyCancel", lambda: state.update(done=True, data=None))
        page.set_content(html)
        try:
            while not state.get("done"):
                if page.is_closed():
                    break
                page.wait_for_timeout(120)
        except Exception:
            pass  # window closed mid-wait
        try:
            browser.close()
        except Exception:
            pass
    return state.get("data")
