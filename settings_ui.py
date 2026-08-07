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
  /* Several rules below set display on things the page also hides, and a class
     rule beats the browser's own [hidden] rule, so make hiding win outright. */
  [hidden] { display: none !important; }
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
  .cities .tog { padding-right: 6px; }
  .tog .lbl { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
  .tog-x {
    flex: none; background: none; border: 0; cursor: pointer; color: var(--ink-soft);
    font: 10pt var(--type); letter-spacing: .1em; padding: 3px 6px; border-radius: 2px;
    opacity: 0; transition: opacity .12s;
  }
  .tog:hover .tog-x, .tog-x:focus { opacity: 1; }
  .tog-x:hover { color: #241608; background: var(--accent); }
  .tog-x[data-confirm] { opacity: 1; color: var(--accent); }

  .addcity { display: flex; gap: 10px; margin-top: 14px; align-items: stretch; }
  .addcity input { flex: 1; min-width: 0; }
  .addcity input:first-child { flex: 0 0 190px; }
  .addcity .mini { flex: none; padding-left: 18px; padding-right: 18px; }
  .addcity-msg { margin-top: 8px; }
  .addcity-msg.bad { color: var(--accent); }
  details.help { margin-top: 12px; }
  details.help summary {
    cursor: pointer; color: var(--accent); font: 10pt var(--type);
    letter-spacing: .12em; text-transform: uppercase;
  }
  details.help .hint { margin-top: 10px; }
  details.help ol { margin: 8px 0 0; padding-left: 20px; }
  details.help li { margin-bottom: 6px; }
  .mini {
    background: none; border: 1px solid rgba(237,146,107,.45); color: var(--accent);
    border-radius: 2px; padding: 5px 11px; cursor: pointer;
    font: 10pt var(--type); letter-spacing: .14em; text-transform: uppercase;
  }
  .mini:hover { background: var(--accent); color: #241608; }
  .seclab { display: flex; align-items: center; justify-content: space-between; }
  .seclab .grp { display: flex; gap: 7px; margin-bottom: 14px; }
  #hiddenWhenNoDescriptions[hidden] { display: none; }

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
  .blocked {
    border-left: 3px solid var(--accent); background: rgba(237,146,107,.09);
    padding: 12px 15px; margin: 0 0 14px; font-size: 11.5pt; line-height: 1.55;
  }
  .blocked p { margin: 0 0 9px; }
  .blocked p:last-child { margin: 0; }
  .blocked code { word-break: break-all; }

  /* Tabs */
  .tabs { display: flex; gap: 0; margin-top: 14px; }
  .tabs button {
    background: none; border: 0; border-bottom: 3px solid transparent;
    color: var(--ink-soft); cursor: pointer; padding: 9px 0; margin-right: 26px;
    font-family: var(--type); font-size: 10pt; letter-spacing: .18em;
    text-transform: uppercase;
  }
  .tabs button:hover { color: var(--ink); }
  .tabs button[aria-selected="true"] {
    color: var(--accent); border-bottom-color: var(--accent); font-weight: 700;
  }
  .pane[hidden] { display: none; }

  /* Dropdowns. appearance:none is what lets the OS widget match the palette. */
  select {
    width: 100%; padding: 10px 34px 10px 12px; color: var(--ink);
    background: var(--olive-dk); border: 1px solid var(--rule);
    border-radius: 2px; font-family: var(--type); font-size: 11pt;
    appearance: none; cursor: pointer;
    background-image: linear-gradient(45deg, transparent 50%, var(--accent) 50%),
                      linear-gradient(135deg, var(--accent) 50%, transparent 50%);
    background-position: calc(100% - 18px) 55%, calc(100% - 13px) 55%;
    background-size: 5px 5px, 5px 5px; background-repeat: no-repeat;
  }
  select:focus { outline: none; border-color: var(--accent); }
  .every { display: flex; gap: 12px; align-items: flex-start; }
  .every .num { flex: 0 0 110px; }
  .every .unit { flex: 0 0 190px; }

  /* Saved search cards */
  .saved { display: flex; flex-direction: column; gap: 12px; }
  .card {
    background: var(--olive-dk); border: 1px solid var(--rule);
    border-radius: 3px; padding: 14px 16px;
  }
  .card.off { opacity: .6; }
  .card .top {
    display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap;
  }
  .card .nm {
    font-family: var(--type); font-size: 11.5pt; font-weight: 700;
    letter-spacing: .1em; text-transform: uppercase; color: var(--ink);
  }
  .card .pill {
    font-family: var(--type); font-size: 9pt; letter-spacing: .14em;
    text-transform: uppercase; color: var(--accent);
    border: 1px solid rgba(237,146,107,.45); border-radius: 2px; padding: 2px 8px;
  }
  .card .pill.paused { color: var(--ink-soft); border-color: var(--rule); }
  .card .det {
    color: var(--ink-soft); font-size: 11pt; margin-top: 8px; line-height: 1.6;
  }
  .card .acts { display: flex; gap: 7px; margin-top: 13px; flex-wrap: wrap; }
  .empty {
    color: var(--ink-soft); font-style: italic; padding: 18px 0; font-size: 11.5pt;
  }
  .note {
    border-left: 3px solid var(--accent); background: rgba(237,146,107,.08);
    padding: 10px 14px; margin-top: 14px; font-size: 11pt; line-height: 1.6;
    color: var(--ink); white-space: pre-line;
  }
  .note.ok { border-left-color: #8FA85C; background: rgba(143,168,92,.1); }
  .note.bad { border-left-color: var(--accent); }
  .note[hidden] { display: none; }
  .schedstate {
    display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
    margin-bottom: 4px;
  }
  .dot {
    width: 9px; height: 9px; border-radius: 50%; background: var(--ink-soft);
    flex: none;
  }
  .dot.on { background: #8FA85C; }
  .dot.stuck { background: var(--accent); }
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
  <div class="tabs" role="tablist">
    <button id="tabNew" role="tab" aria-selected="true">New search</button>
    <button id="tabSaved" role="tab" aria-selected="false">Saved searches</button>
    <button id="tabEmail" role="tab" aria-selected="false">Email &amp; schedule</button>
  </div>
</header>
<main>
<div class="pane" id="paneNew">
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

    <div class="addcity">
      <input type="text" id="new_city_label" placeholder="Denver, CO">
      <input type="text" id="new_city_url"
             placeholder="paste the Marketplace link for that city">
      <button class="mini" id="addCity">Add city</button>
    </div>
    <div class="hint addcity-msg" id="addCityMsg">Cities you add are saved in your
      own file, and you can remove them again by hovering over them. The twelve
      the app comes with are permanent — untick one to skip it for a run.</div>

    <details class="help">
      <summary>How to get a city's link</summary>
      <div class="hint">
        <ol>
          <li>Open <b>facebook.com/marketplace</b> in your browser.</li>
          <li>Click the location on the left sidebar — it shows whatever
            city you're currently browsing.</li>
          <li>Type the city you want, pick it from the list, set the radius to
            <b>500 miles</b>, and click Apply.</li>
          <li>Copy the whole web address out of the address bar and paste it in
            the box above. It looks like
            <b>facebook.com/marketplace/denver/...</b> — the part right after
            "marketplace" is what this needs, and it's fine if it's a long
            number instead of a name.</li>
        </ol>
        <p style="margin: 10px 0 0">Leave the name box blank and it'll name the
          city after that link.</p>
      </div>
    </details>
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
      <div class="tog" id="do_descriptions" role="button" tabindex="0" aria-pressed="true">
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

  <section id="descBlock">
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
        <input type="number" id="descriptions_budget" value="__BUDGET__"
               placeholder="never ask" min="0">
        <div class="hint">If retrieving descriptions for all listings would take
          longer than this many minutes, the run pauses in the terminal and
          offers you three choices: retrieve descriptions for as many as it can
          in the allotted time, do all of them anyway, or skip description
          retrieval. Leave it blank to never ask.</div>
      </label>
    </div>
  </section>

  <section id="saveBlock">
    <h2>Run this on a schedule</h2>
    <div class="hint" style="margin: 0 0 16px">Save these settings under a name
      and this search will run itself, then email you what it found. It only
      fetches descriptions and photos for listings it hasn't seen before, so
      later runs are much quicker than the first one.</div>
    <label class="field"><span class="lab">Name this search</span>
      <input type="text" id="save_name" placeholder="Defender 110">
    </label>
    <label class="field"><span class="lab">Email the report to</span>
      <input type="text" id="save_email" placeholder="you@example.com">
      <div class="hint">Leave blank to use the address on the
        <b>Email &amp; schedule</b> tab.</div>
    </label>
    <div class="lab">How often</div>
    <div class="every">
      <div class="num"><input type="number" id="save_every" value="1" min="1"></div>
      <div class="unit"><select id="save_unit"></select></div>
    </div>
    <div class="note" id="saveWarn" hidden></div>
    <div class="note" id="saveMsg" hidden></div>
    <div class="acts" style="display:flex; gap:9px; margin-top:16px">
      <button class="mini" id="saveSearch">Save scheduled search</button>
      <button class="mini" id="cancelEdit" hidden>Stop editing</button>
    </div>
  </section>
</div>

<div class="pane" id="paneSaved" hidden>
  <section>
    <div class="seclab"><h2>Saved searches</h2>
      <div class="grp"><button class="mini" id="refreshSaved">Refresh</button></div>
    </div>
    <div class="hint" style="margin: 0 0 16px">These run on their own and email
      you a report. Editing one loads it back into the <b>New search</b> tab.</div>
    <div class="saved" id="savedList"></div>
    <div class="note" id="savedMsg" hidden></div>
  </section>
</div>

<div class="pane" id="paneEmail" hidden>
  <section>
    <h2>Automatic runs</h2>
    <div class="schedstate">
      <span class="dot" id="schedDot"></span>
      <span class="lab" style="margin:0" id="schedState">Checking…</span>
    </div>
    <div class="hint" style="margin: 10px 0 14px" id="schedHint"></div>
    <div class="blocked" id="schedProblem" hidden></div>
    <div class="acts" style="display:flex; gap:9px">
      <button class="mini" id="schedOn">Turn automatic runs on</button>
      <button class="mini" id="schedOff">Turn them off</button>
    </div>
    <div class="note" id="schedMsg" hidden></div>
  </section>

  <section>
    <h2>Email</h2>
    <div class="hint" style="margin: 0 0 16px">Reports are sent from your own
      email account. For Gmail you need an <b>app password</b>, not your normal
      password — see the steps below. An app password gives this tool full access
      to that mailbox, so it's stored in <code>email_config.json</code>, which is
      kept out of version control.</div>
    <label class="field"><span class="lab">Your email address</span>
      <input type="text" id="mail_address" placeholder="you@gmail.com">
    </label>
    <label class="field"><span class="lab">App password</span>
      <input type="text" id="mail_password" placeholder="sixteen letters from Google">
    </label>
    <div class="row">
      <label class="field"><span class="lab">Provider</span>
        <select id="mail_provider">
          <option value="gmail">Gmail</option>
          <option value="outlook">Outlook</option>
          <option value="icloud">iCloud</option>
          <option value="other">Other (set server below)</option>
        </select>
      </label>
      <label class="field"><span class="lab">Send reports to</span>
        <input type="text" id="mail_to" placeholder="same as your address">
      </label>
    </div>
    <div class="row" id="mailHostRow" hidden>
      <label class="field"><span class="lab">SMTP server</span>
        <input type="text" id="mail_host" placeholder="mail.example.com"></label>
      <label class="field"><span class="lab">Port</span>
        <input type="number" id="mail_port" placeholder="587" min="1"></label>
    </div>
    <div class="acts" style="display:flex; gap:9px; margin-top:4px">
      <button class="mini" id="saveMail">Save</button>
      <button class="mini" id="testMail">Send a test email</button>
    </div>
    <div class="note" id="mailMsg" hidden></div>

    <details class="help">
      <summary>How to get a Gmail app password</summary>
      <div class="hint">
        <ol>
          <li>Go to <b>myaccount.google.com</b> and sign in.</li>
          <li>Open <b>Security & sign-in</b> in the left sidebar.</li>
          <li>Turn on <b>2-Step Verification</b> if it isn't already. App
            passwords don't work without it.</li>
          <li>Search that page for <b>App passwords</b> and open it.</li>
          <li>Type a name like <b>Faceplace Marketbook</b> and click <b>Create</b>.</li>
          <li>Google shows a sixteen-letter password. Copy it into the
            box above. You can include the spaces or leave them out.</li>
        </ol>
        <p style="margin: 10px 0 0">Because you're sending to yourself, Gmail may
          file the report under <b>Sent Mail</b> instead of your inbox. Click
          <b>Send a test email</b> and this will check where it landed and fix it
          if needed.</p>
      </div>
    </details>
  </section>
</div>
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
const BUILTINS = __BUILTINS__;
const PACES = __PACES__;
const DEFAULTS = __DEFAULTS__;
const SAVED = __SAVED__;
const EMAIL = __EMAIL__;
const UNITS = __UNITS__;
// Fixed per-listing costs no pace setting can remove: loading the page and
// reading its payload, plus saving the photo when thumbnails are on.
const PAGE_WORK = DEFAULTS.page_work || 3.5;
const PHOTO_SAVE = DEFAULTS.photo_save || 1.5;

const $ = id => document.getElementById(id);
const secsPer = (p, withThumbs) =>
  PAGE_WORK + (withThumbs ? PHOTO_SAVE : 0) + (PACES[p][0] + PACES[p][1]) / 2;

// Cities
const cityWrap = $('cities');
const escHtml = s => String(s).replace(/[&<>"]/g,
  m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m]));

function renderCities(labels, selected) {
  // `selected` is a Set when we're re-rendering after an add or remove, so a
  // change to the list doesn't quietly re-check cities the user turned off.
  cityWrap.innerHTML = '';
  labels.forEach(label => {
    const d = document.createElement('div');
    d.className = 'tog'; d.dataset.city = label; d.setAttribute('role', 'button');
    d.tabIndex = 0;
    d.setAttribute('aria-pressed', !selected || selected.has(label) ? 'true' : 'false');
    // The cities that ship with the app have no remove button: they're spaced to
    // cover the country, and unchecking one skips it just as well.
    d.innerHTML = `<span class="box">✓</span><span class="lbl">${escHtml(label)}</span>`
      + (BUILTINS.includes(label)
         ? '' : `<button class="tog-x" title="Remove this city">✕</button>`);
    cityWrap.appendChild(d);
  });
}
const selectedCities = () => new Set([...cityWrap.querySelectorAll('.tog')]
  .filter(t => t.getAttribute('aria-pressed') === 'true')
  .map(t => t.dataset.city));
renderCities(LOCATIONS);

function sayCity(msg, bad) {
  const el = $('addCityMsg');
  el.textContent = msg;
  el.classList.toggle('bad', !!bad);
}

$('addCity').onclick = async () => {
  const btn = $('addCity'), label = $('new_city_label').value.trim();
  const url = $('new_city_url').value.trim();
  btn.disabled = true;
  sayCity('Adding…');
  const res = await window.pyAddCity(label, url);
  btn.disabled = false;
  if (res.error) { sayCity(res.error, true); return; }
  const keep = selectedCities();
  res.cities.filter(c => !LOCATIONS.includes(c)).forEach(c => keep.add(c));
  LOCATIONS.length = 0; LOCATIONS.push(...res.cities);
  renderCities(res.cities, keep);
  $('new_city_label').value = ''; $('new_city_url').value = '';
  sayCity(`Added ${res.added}. It'll be here next time too.`);
  refresh();
};
['new_city_label', 'new_city_url'].forEach(id =>
  $(id).addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); $('addCity').click(); }
  }));

// Removing edits locations.json on disk, so it asks for a second click rather
// than deleting a city out from under a stray cursor.
cityWrap.addEventListener('click', async e => {
  const x = e.target.closest('.tog-x');
  if (!x) return;
  e.stopPropagation();
  const label = x.closest('.tog').dataset.city;
  if (!x.dataset.confirm) {
    cityWrap.querySelectorAll('.tog-x[data-confirm]').forEach(o => {
      delete o.dataset.confirm; o.textContent = '✕';
    });
    x.dataset.confirm = '1'; x.textContent = 'Remove?';
    return;
  }
  const res = await window.pyRemoveCity(label);
  const keep = selectedCities();
  if (!res.error) keep.delete(label);
  LOCATIONS.length = 0; LOCATIONS.push(...res.cities);
  renderCities(res.cities, keep);
  sayCity(res.error || `Removed ${label}.`, !!res.error);
  refresh();
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
    do_descriptions: on('do_descriptions'),
    do_thumbs: on('do_thumbs'),
    do_gallery: on('do_gallery'),
    debug_dump: on('debug_dump'),
    pace: pace,
    limit: num('limit'),
    descriptions_budget: num('descriptions_budget'),
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
  $('descBlock').hidden = !c.do_descriptions;
  const parts = [`<b>${c.cities.length}</b> ${c.cities.length === 1 ? 'city' : 'cities'}`];
  if (c.do_descriptions) {
    parts.push(`<b>${Math.round(secsPer(pace, c.do_thumbs))}s</b> per listing`);
    parts.push(c.limit ? `<b>${c.limit}</b> max` : `<b>all</b> listings`);
  } else parts.push('no descriptions');
  $('est').innerHTML = parts.join(' &middot; ')
    + (c.query ? '' : ' &middot; <span class="warn">query required</span>');
  $('start').disabled = !c.query || c.cities.length === 0;
}

$('query').addEventListener('input', refresh);
['min_price','max_price','exclude','limit','descriptions_budget']
  .forEach(id => $(id).addEventListener('input', refresh));

$('start').onclick = () => {
  $('start').disabled = true;
  window.pySubmit({action: 'sweep', ...collect()});
};
$('cancel').onclick = () => window.pyCancel();
document.addEventListener('keydown', e => {
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && !$('start').disabled
      && tab === 'new') $('start').click();
  if (e.key === 'Escape') window.pyCancel();
});

// ---------------------------------------------------------------- tabs
let tab = 'new';
const PANES = {new: 'paneNew', saved: 'paneSaved', email: 'paneEmail'};
const TABS = {new: 'tabNew', saved: 'tabSaved', email: 'tabEmail'};

function showTab(which) {
  tab = which;
  Object.entries(PANES).forEach(([k, id]) => { $(id).hidden = k !== which; });
  Object.entries(TABS).forEach(([k, id]) =>
    $(id).setAttribute('aria-selected', k === which ? 'true' : 'false'));
  // Start sweep only means something on the search tab.
  $('start').hidden = which !== 'new';
  $('est').hidden = which !== 'new';
  $('cancel').textContent = which === 'new' ? 'Cancel' : 'Close';
  if (which === 'saved') renderSaved();
  if (which === 'email') loadSchedState();
}
Object.entries(TABS).forEach(([k, id]) => { $(id).onclick = () => showTab(k); });

function say(id, msg, kind) {
  const el = $(id);
  el.hidden = !msg;
  el.textContent = msg || '';
  el.className = 'note' + (kind ? ' ' + kind : '');
}

// ------------------------------------------------- saving a scheduled search
UNITS.forEach(u => {
  const o = document.createElement('option');
  o.value = u;
  o.textContent = u.charAt(0).toUpperCase() + u.slice(1);
  $('save_unit').appendChild(o);
});
$('save_unit').value = UNITS.includes('days') ? 'days' : UNITS[0];

let editingId = null;

function scheduleFields() {
  return {
    name: $('save_name').value.trim(),
    email_to: $('save_email').value.trim(),
    interval: {every: Number($('save_every').value) || 1,
               unit: $('save_unit').value},
  };
}

async function checkWarnings() {
  const res = await window.pyCheckSchedule({...scheduleFields(), id: editingId});
  say('saveWarn', (res.warnings || []).join(' '), 'bad');
}
['save_every', 'save_unit'].forEach(id =>
  $(id).addEventListener('change', checkWarnings));

$('saveSearch').onclick = async () => {
  const btn = $('saveSearch');
  btn.disabled = true;
  say('saveMsg', 'Saving…');
  const payload = {...collect(), ...scheduleFields(), id: editingId};
  const res = await window.pySaveSearch(payload);
  btn.disabled = false;
  if (res.error) { say('saveMsg', res.error, 'bad'); return; }
  say('saveWarn', (res.warnings || []).join(' '), 'bad');
  say('saveMsg', res.message, 'ok');
  SAVED.length = 0; SAVED.push(...(res.searches || []));
  stopEditing();
};

function startEditing(s) {
  editingId = s.id;
  $('query').value = s.query || '';
  $('exclude').value = s.exclude || '';
  $('min_price').value = s.min_price == null ? '' : s.min_price;
  $('max_price').value = s.max_price == null ? '' : s.max_price;
  $('limit').value = s.limit == null ? '' : s.limit;
  $('exact').setAttribute('aria-pressed', s.exact ? 'true' : 'false');
  $('do_descriptions').setAttribute('aria-pressed',
    s.do_descriptions === false ? 'false' : 'true');
  $('do_thumbs').setAttribute('aria-pressed',
    s.do_thumbs === false ? 'false' : 'true');
  pace = s.pace || 'fast';
  $('save_name').value = s.name || '';
  $('save_email').value = s.email_to || '';
  $('save_every').value = (s.interval && s.interval.every) || 1;
  $('save_unit').value = (s.interval && s.interval.unit) || 'days';
  const keep = new Set(s.cities || []);
  cityWrap.querySelectorAll('.tog').forEach(t =>
    t.setAttribute('aria-pressed', keep.has(t.dataset.city) ? 'true' : 'false'));
  $('saveSearch').textContent = 'Update saved search';
  $('cancelEdit').hidden = false;
  say('saveMsg', `Editing “${s.name}”. Change anything above, then update it.`);
  showTab('new');
  refresh();
  checkWarnings();
}

function stopEditing() {
  editingId = null;
  $('saveSearch').textContent = 'Save scheduled search';
  $('cancelEdit').hidden = true;
}
$('cancelEdit').onclick = () => { stopEditing(); say('saveMsg', ''); };

// ------------------------------------------------------- the saved searches list
function renderSaved() {
  const wrap = $('savedList');
  if (!SAVED.length) {
    wrap.innerHTML = '<div class="empty">No saved searches yet. Set one up at '
      + 'the bottom of the New search tab.</div>';
    return;
  }
  wrap.innerHTML = SAVED.map(s => `
    <div class="card${s.enabled ? '' : ' off'}" data-id="${escHtml(s.id)}">
      <div class="top">
        <span class="nm">${escHtml(s.name)}</span>
        <span class="pill${s.enabled ? '' : ' paused'}">${
          s.enabled ? escHtml(s.every_text) : 'paused'}</span>
      </div>
      <div class="det">
        “${escHtml(s.query)}” across ${(s.cities || []).length} ${
          (s.cities || []).length === 1 ? 'city' : 'cities'}<br>
        Last run ${escHtml(s.last_text)} &middot; next ${escHtml(s.next_text)}<br>
        ${s.tracking == null ? '' : escHtml(String(s.tracking)) + ' listings tracked'}
        ${s.email_to ? ' &middot; reports to ' + escHtml(s.email_to) : ''}
      </div>
      <div class="acts">
        <button class="mini" data-act="run">Run now</button>
        <button class="mini" data-act="edit">Edit</button>
        <button class="mini" data-act="toggle">${s.enabled ? 'Pause' : 'Resume'}</button>
        <button class="mini" data-act="del">Delete</button>
      </div>
    </div>`).join('');
}

$('refreshSaved').onclick = async () => {
  const res = await window.pyListSearches();
  if (res.error) { say('savedMsg', res.error, 'bad'); return; }
  SAVED.length = 0; SAVED.push(...(res.searches || []));
  renderSaved();
  say('savedMsg', '');
};

$('savedList').addEventListener('click', async e => {
  const btn = e.target.closest('button[data-act]');
  if (!btn) return;
  const card = btn.closest('.card');
  const id = card.dataset.id;
  const s = SAVED.find(x => x.id === id);
  const act = btn.dataset.act;

  if (act === 'edit') { startEditing(s); return; }
  if (act === 'run') {
    // Running needs the browser and the Facebook session, so the window has to
    // close first — it's holding the only Chromium this tool can use.
    window.pySubmit({action: 'run_saved', id: id});
    return;
  }
  if (act === 'toggle') {
    const res = await window.pyUpdateSearch(id, {enabled: !s.enabled});
    if (res.error) { say('savedMsg', res.error, 'bad'); return; }
    SAVED.length = 0; SAVED.push(...(res.searches || []));
    renderSaved();
    say('savedMsg', `“${s.name}” is now ${s.enabled ? 'paused' : 'active'}.`, 'ok');
    return;
  }
  if (act === 'del') {
    // Deleting is permanent, so it asks for a second click.
    if (!btn.dataset.confirm) {
      $('savedList').querySelectorAll('button[data-confirm]').forEach(o => {
        delete o.dataset.confirm; o.textContent = 'Delete';
      });
      btn.dataset.confirm = '1';
      btn.textContent = 'Really delete?';
      return;
    }
    const res = await window.pyDeleteSearch(id);
    if (res.error) { say('savedMsg', res.error, 'bad'); return; }
    SAVED.length = 0; SAVED.push(...(res.searches || []));
    if (editingId === id) stopEditing();
    renderSaved();
    say('savedMsg', `Deleted “${s.name}”. Its results folder is still on disk.`,
        'ok');
  }
});

// -------------------------------------------------------------- email settings
function fillMail(cfg) {
  $('mail_address').value = cfg.address || '';
  $('mail_password').value = cfg.app_password || '';
  $('mail_to').value = cfg.default_to || '';
  $('mail_provider').value = cfg.provider || 'gmail';
  $('mail_host').value = cfg.host || '';
  $('mail_port').value = cfg.port || '';
  mailHostRow();
}
function mailHostRow() {
  $('mailHostRow').hidden = $('mail_provider').value !== 'other';
}
$('mail_provider').addEventListener('change', mailHostRow);
fillMail(EMAIL);

$('saveMail').onclick = async () => {
  const res = await window.pySaveEmail({
    address: $('mail_address').value.trim(),
    app_password: $('mail_password').value.trim(),
    default_to: $('mail_to').value.trim(),
    provider: $('mail_provider').value,
    host: $('mail_host').value.trim(),
    port: Number($('mail_port').value) || 587,
  });
  say('mailMsg', res.error || res.message, res.error ? 'bad' : 'ok');
};

$('testMail').onclick = async () => {
  const btn = $('testMail');
  btn.disabled = true;
  say('mailMsg', 'Sending…');
  const res = await window.pyTestEmail();
  btn.disabled = false;
  say('mailMsg', res.error || res.message, res.error ? 'bad' : 'ok');
};

// ------------------------------------------------------------ automatic runs
async function loadSchedState() {
  const res = await window.pyScheduleState();
  // Installed and blocked is its own state: calling it "on" would contradict the
  // instructions sitting right underneath.
  const stuck = res.installed && (res.problems || []).length > 0;
  $('schedDot').className = 'dot' + (stuck ? ' stuck' : res.installed ? ' on' : '');
  $('schedState').textContent = !res.installed ? 'Automatic runs are off'
    : stuck ? 'Automatic runs are on, but blocked' : 'Automatic runs are on';
  $('schedHint').innerHTML = res.hint || '';
  showProblems(res.problems);
  $('schedOn').hidden = res.installed;
  $('schedOff').hidden = !res.installed;
}

// Paths in these messages have to be copyable, so they're set as text inside
// <code> rather than pasted into innerHTML.
function showProblems(problems) {
  const box = $('schedProblem');
  box.textContent = '';
  box.hidden = !(problems && problems.length);
  (problems || []).forEach(text => {
    const p = document.createElement('p');
    text.split('\\n').forEach((line, i) => {
      if (i) p.appendChild(document.createElement('br'));
      const bare = line.trim();
      if (i && bare.startsWith('/')) {
        const c = document.createElement('code');
        c.textContent = bare;
        p.appendChild(c);
      } else {
        p.appendChild(document.createTextNode(line));
      }
    });
    box.appendChild(p);
  });
}
$('schedOn').onclick = async () => {
  say('schedMsg', 'Setting it up, this takes a few seconds…');
  const res = await window.pySetSchedule(true);
  await loadSchedState();
  if (res.ok) {
    say('schedMsg', (res.messages || []).join('\\n\\n'), 'ok');
  } else {
    // A refusal is several paragraphs of instructions, so it belongs in the
    // problem box rather than a one-line status. Rendered after the state
    // reload so the reload can't wipe it.
    $('schedMsg').hidden = true;
    showProblems(res.messages);
  }
};
$('schedOff').onclick = async () => {
  const res = await window.pySetSchedule(false);
  await loadSchedState();
  say('schedMsg', (res.messages || []).join('\\n\\n'), 'ok');
};

if (DEFAULTS.query) $('query').value = DEFAULTS.query;
if (DEFAULTS.exclude) $('exclude').value = DEFAULTS.exclude;
showTab('new');
refresh();
$('query').focus();
</script>
</body></html>
"""


def _call(hooks, name, default=None):
    """Reads a value out of a hook for the initial page render. A missing or
    broken hook must not stop the settings window from opening at all."""
    fn = hooks.get(name)
    if not fn:
        return default
    try:
        return fn()
    except Exception:
        return default


def render(locations, paces, defaults, saved=(), email=None,
           units=("hours", "days"), builtins=()):
    defaults = dict(defaults or {})
    # 0 / None means "never ask", which the form shows as an empty field.
    budget = defaults.get("descriptions_budget") or ""
    return (HTML
            .replace("__FONTS__", FONTS)
            .replace("__CSS__", CSS)
            .replace("__BUDGET__", str(budget))
            .replace("__BUILTINS__", json.dumps(list(builtins)))
            .replace("__LOCATIONS__", json.dumps(list(locations)))
            .replace("__PACES__", json.dumps(paces))
            .replace("__SAVED__", json.dumps(list(saved)))
            .replace("__EMAIL__", json.dumps(email or {}))
            .replace("__UNITS__", json.dumps(list(units)))
            .replace("__DEFAULTS__", json.dumps(defaults)))


def collect_settings(locations, paces, defaults=None, headless=False,
                     on_add=None, on_remove=None, hooks=None, on_ready=None,
                     builtins=()):
    """Opens the settings window and blocks until it's done.

    `on_add(label, text)` and `on_remove(label)` should both return
    (labels, error) and persist the change. Without them the city list is
    read-only. Cities named in `builtins` get no remove button.

    `hooks` wires up the saved-search and email tabs. Every entry is optional;
    whatever is missing simply leaves that part of the window inert, so this
    module still works with nothing but the search form. See scheduling.ui_hooks
    for the implementations.

    `on_ready(page)` is called once the page is loaded, which is how the test
    suite clicks through this window without a person in front of it.

    Returns whatever the page submitted — a dict with an "action" key — or None
    if cancelled or the window was closed.
    """
    hooks = dict(hooks or {})
    html = render(locations, paces, defaults,
                  saved=_call(hooks, "list_searches", default={}).get("searches", []),
                  email=_call(hooks, "email_config", default={}),
                  units=hooks.get("units") or ("hours", "days"),
                  builtins=builtins)
    state = {}
    known = list(locations)

    def add_city(label, text):
        if not on_add:
            return {"error": "Adding cities isn't available here."}
        try:
            labels, error = on_add(label, text)
        except Exception as e:
            return {"error": f"Couldn't save that: {e}"}
        if error:
            return {"error": error}
        labels = list(labels)
        added = next((c for c in labels if c not in known), label)
        known[:] = labels
        return {"cities": labels, "added": added}

    def remove_city(label):
        if not on_remove:
            return {"cities": list(known)}
        try:
            labels, error = on_remove(label)
        except Exception as e:
            return {"cities": list(known), "error": f"Couldn't remove that: {e}"}
        known[:] = list(labels)
        return {"cities": list(known), "error": error}

    def hook(name):
        """Wraps a hook so a bug in it shows up as a message in the window
        rather than an exception the page never hears back from — an unanswered
        expose_function call leaves the button spinning forever."""
        def call(*args):
            fn = hooks.get(name)
            if not fn:
                return {"error": "That isn't available in this window."}
            try:
                return fn(*args)
            except Exception as e:
                return {"error": f"{type(e).__name__}: {e}"}
        return call

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless,
                                    args=["--window-size=1000,1180"])
        page = browser.new_page(**({} if not headless
                                   else {"viewport": {"width": 1000, "height": 1180}}),
                                no_viewport=not headless)
        # First answer wins: a stray second click, or a close that races a
        # submit, must not replace what the user already asked for.
        def finish(data):
            if not state.get("done"):
                state.update(done=True, data=data)

        page.expose_function("pySubmit", finish)
        page.expose_function("pyCancel", lambda: finish(None))
        page.expose_function("pyAddCity", add_city)
        page.expose_function("pyRemoveCity", remove_city)
        for js_name, hook_name in (
                ("pyListSearches", "list_searches"),
                ("pySaveSearch", "save_search"),
                ("pyUpdateSearch", "update_search"),
                ("pyDeleteSearch", "delete_search"),
                ("pyCheckSchedule", "check_schedule"),
                ("pySaveEmail", "save_email"),
                ("pyTestEmail", "test_email"),
                ("pyScheduleState", "schedule_state"),
                ("pySetSchedule", "set_schedule")):
            page.expose_function(js_name, hook(hook_name))
        page.set_content(html)
        if on_ready:
            on_ready(page)
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
