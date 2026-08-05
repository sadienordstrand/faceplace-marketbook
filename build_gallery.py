#!/usr/bin/env python3
"""
build_gallery.py
-----------------
Turn a results CSV into a single self-contained HTML gallery.

    python3 build_gallery.py marketplace_results.csv

Writes gallery.html next to the CSV. Locally downloaded thumbnails are baked
into the file as data URIs by default, so the gallery is one portable file
with no dependency on the thumbs/ folder, relative paths, or Facebook URLs.
Pass --no-embed to link the thumbnails instead and keep the file small.
"""
import argparse
import base64
import csv
import json
import mimetypes
from pathlib import Path

EMBED_BUDGET_MB = 60

HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Faceplace Marketbook</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet"
      href="https://fonts.googleapis.com/css2?family=Lato:ital,wght@0,400;0,700;1,400&family=Special+Elite&family=Courier+Prime:wght@400;700&display=swap">
<style>
  :root {
    --olive: #373D1F; --olive-dk: #262B14; --olive-lt: #5C6537;
    --accent: #ED926B; --accent-soft: #F0AC8E;
    --bg: #16180F; --card: #212418; --card-hi: #2A2E20;
    --ink: #EDE3CE; --ink-soft: #A79E85; --rule: #3A3F2B;
    --display: "Iowan Old Style", "Palatino Linotype", Palatino,
               "Book Antiqua", Georgia, serif;
    --body: "Lato", -apple-system, BlinkMacSystemFont, "Helvetica Neue",
            Arial, sans-serif;
    /* Typewriter faces: Special Elite (distressed) for the masthead,
       Courier Prime (clean) for small UI type. */
    --brandface: "Courier Prime", "American Typewriter", "Courier New", monospace;
    --type: "Courier Prime", "American Typewriter", "Courier New", monospace;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 14.5px/1.55 var(--body);
  }
  /* Faint grain, drawn procedurally so the file stays self-contained. */
  body::before {
    content: ""; position: fixed; inset: 0; z-index: 100; pointer-events: none;
    opacity: .5; mix-blend-mode: soft-light;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='3'/%3E%3C/filter%3E%3Crect width='160' height='160' filter='url(%23n)' opacity='0.22'/%3E%3C/svg%3E");
  }

  header {
    position: sticky; top: 0; z-index: 20;
    background: var(--olive); color: var(--ink);
    border-bottom: 5px solid var(--accent);
    padding: 16px 26px 14px;
    display: flex; flex-wrap: wrap; gap: 12px; align-items: center;
    box-shadow: 0 3px 14px rgba(0, 0, 0, .4);
  }
  .brand {
    margin: 0 auto 0 0; font-family: var(--brandface); font-size: 21px;
    font-weight: 700; letter-spacing: .16em; text-transform: uppercase;
    color: #F5EEDC;
  }
  .controls { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }

  .tally {
    display: flex; align-items: center; justify-content: center; gap: 14px;
    padding: 26px 20px 0; color: var(--ink-soft);
    font-family: var(--type); font-size: 12px; letter-spacing: .22em;
    text-transform: uppercase;
  }
  .tally button {
    background: none; color: var(--accent); cursor: pointer;
    border: 1px solid rgba(237, 146, 107, .45); border-radius: 2px;
    padding: 4px 10px; font: 10.5px var(--type);
    letter-spacing: .16em; text-transform: uppercase;
  }
  .tally button:hover { background: var(--accent); color: #241608; }

  input[type=search] {
    width: 250px; background: var(--bg); color: var(--ink);
    border: 1px solid var(--olive-dk); border-radius: 2px;
    padding: 8px 12px; font: 13px var(--type); outline: none;
    box-shadow: inset 0 1px 4px rgba(0, 0, 0, .35);
  }
  input[type=search]::placeholder { color: #FFFFFF; opacity: .95; }
  input[type=search]:focus {
    border-color: var(--accent); box-shadow: 0 0 0 2px rgba(237, 146, 107, .35);
  }

  /* Custom dropdown: a native <select> popup is drawn by the OS, so it can't
     be positioned or themed to match this palette. */
  .sel { position: relative; }
  .sel-btn {
    display: flex; align-items: center; gap: 14px;
    background: var(--bg); color: var(--ink);
    border: 1px solid var(--olive-dk); border-radius: 2px;
    padding: 8px 14px; font: 13px var(--type); cursor: pointer;
    box-shadow: inset 0 1px 4px rgba(0, 0, 0, .35);
  }
  .sel-btn:hover { border-color: var(--olive-lt); }
  .sel-btn:focus-visible {
    outline: none; border-color: var(--accent);
    box-shadow: 0 0 0 2px rgba(237, 146, 107, .35);
  }
  .sel-btn .caret {
    margin-left: auto; width: 0; height: 0; flex: none;
    border-left: 4.5px solid transparent; border-right: 4.5px solid transparent;
    border-top: 5px solid var(--accent); transition: transform .15s ease;
  }
  .sel-btn[aria-expanded="true"] .caret { transform: rotate(180deg); }
  .sel-list {
    position: absolute; top: calc(100% + 6px); left: 0; z-index: 30;
    min-width: 100%; max-height: 320px; overflow-y: auto;
    margin: 0; padding: 4px; list-style: none;
    background: var(--card-hi); color: var(--ink);
    border: 1px solid var(--rule); border-radius: 2px;
    box-shadow: 0 10px 26px rgba(0, 0, 0, .55);
  }
  .sel-list li {
    padding: 7px 12px; font: 13px var(--type); white-space: nowrap;
    border-radius: 2px; cursor: pointer;
  }
  .sel-list li.active { background: var(--olive); }
  .sel-list li.on { color: var(--accent); font-weight: 700; }
  .sel-list li:hover { background: var(--olive-lt); }

  main {
    display: grid; gap: 22px; padding: 18px 26px 44px;
    grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
    max-width: 1600px; margin: 0 auto;
  }
  .card {
    background: var(--card); border: 1px solid var(--rule); border-radius: 3px;
    display: flex; flex-direction: column; overflow: hidden; cursor: pointer;
    box-shadow: 2px 3px 0 rgba(0, 0, 0, .35);
    transition: transform .14s ease, box-shadow .14s ease, border-color .14s ease;
  }
  .card:hover {
    transform: translateY(-3px); border-color: var(--olive-lt);
    box-shadow: 3px 7px 0 rgba(0, 0, 0, .45);
  }
  /* flex: none + min-height: 0 keep the square: as a flex item this box would
     otherwise grow to a tall photo's intrinsic height. */
  .imgwrap {
    aspect-ratio: 1 / 1; position: relative; flex: none; min-height: 0;
    overflow: hidden; background: #14160F; border-bottom: 1px solid var(--rule);
  }
  .imgwrap img {
    position: absolute; inset: 0; width: 100%; height: 100%;
    object-fit: cover; display: block;
    filter: sepia(.2) saturate(.85) brightness(.88); transition: filter .2s ease;
  }
  .card:hover .imgwrap img { filter: none; }
  .hide-btn {
    position: absolute; top: 8px; right: 8px; z-index: 2;
    min-width: 27px; height: 27px; padding: 0 6px; cursor: pointer;
    background: rgba(16, 18, 11, .78); color: var(--ink);
    border: 1px solid var(--rule); border-radius: 2px;
    font: 11px var(--type); line-height: 1; letter-spacing: .1em;
    opacity: 0; transition: opacity .14s ease, background .14s ease;
  }
  .card:hover .hide-btn, .hide-btn:focus-visible { opacity: 1; }
  .hide-btn:hover {
    background: var(--accent); color: #241608; border-color: var(--accent);
  }
  .card.is-hidden { opacity: .45; }
  .card.is-hidden .hide-btn { opacity: 1; }

  /* Applies in both the card and the detail view, so the placeholder always
     lands dead centre of the image area. */
  .noimg {
    position: absolute; inset: 0; display: flex; align-items: center;
    justify-content: center; text-align: center; padding: 12px;
    color: var(--ink-soft); font: 11.5px var(--type);
    letter-spacing: .14em; text-transform: uppercase;
  }
  .body { padding: 13px 15px 15px; display: flex; flex-direction: column; gap: 7px; flex: 1; }
  .price {
    font-family: var(--display); font-size: 22px; font-weight: 700;
    color: var(--accent); line-height: 1; letter-spacing: .01em;
  }
  .title {
    font-family: var(--display); font-size: 16px; font-weight: 600;
    line-height: 1.32;
  }
  .meta {
    color: var(--ink-soft); font-family: var(--type); font-size: 10.5px;
    letter-spacing: .1em; text-transform: uppercase;
  }
  .desc {
    color: #BDB49B; font-size: 12.5px; white-space: pre-line; cursor: pointer;
    border-top: 1px solid var(--rule); padding-top: 7px; margin-top: 1px;
    display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .desc.open { -webkit-line-clamp: unset; }
  .foot { margin-top: auto; padding-top: 11px; }
  .foot a {
    display: inline-block; text-decoration: none; font-size: 10.5px;
    letter-spacing: .13em; text-transform: uppercase; color: var(--accent);
    border: 1px solid rgba(237, 146, 107, .5); border-radius: 2px;
    padding: 6px 12px; transition: background .14s ease, color .14s ease,
    border-color .14s ease;
  }
  .foot a:hover {
    background: var(--accent); border-color: var(--accent); color: #241608;
  }
  .empty {
    grid-column: 1 / -1; text-align: center; color: var(--ink-soft);
    font-style: italic; padding: 70px 0; font-size: 16px;
  }

  /* Detail view */
  .scrim {
    position: fixed; inset: 0; z-index: 200; display: flex;
    align-items: center; justify-content: center; padding: 32px;
    background: rgba(9, 10, 6, .82);
  }
  .scrim[hidden] { display: none; }
  .sheet {
    position: relative;
    background: var(--card); border: 1px solid var(--olive-lt);
    border-top: 5px solid var(--accent); border-radius: 3px;
    width: min(940px, 100%); max-height: 100%; overflow-y: auto;
    display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    box-shadow: 0 22px 60px rgba(0, 0, 0, .7);
  }
  @media (max-width: 760px) { .sheet { grid-template-columns: 1fr; } }
  .sheet-img {
    position: relative; min-height: 320px;
    background: #12140D; border-right: 1px solid var(--rule);
  }
  .sheet-img img { width: 100%; height: 100%; max-height: 78vh; object-fit: contain; display: block; }
  .sheet-body { padding: 24px 26px 26px; display: flex; flex-direction: column; gap: 12px; }
  .sheet-body .price { font-size: 30px; }
  .sheet-body .title { font-size: 22px; line-height: 1.25; }
  .sheet-desc {
    white-space: pre-line; font-size: 13.5px; line-height: 1.6; color: #CBC2A8;
    border-top: 1px solid var(--rule); padding-top: 13px;
  }
  .sheet-desc .lbl, .sheet-none {
    display: block; font: 10.5px var(--type); letter-spacing: .18em;
    text-transform: uppercase; color: var(--ink-soft); margin-bottom: 8px;
  }
  .sheet-none {
    border-top: 1px solid var(--rule); padding-top: 13px; margin-bottom: 0;
  }
  .sheet-close {
    align-self: flex-end; margin-bottom: -4px;
    background: var(--card-hi); color: var(--ink);
    border: 1px solid var(--olive-lt); border-radius: 2px;
    font: 13px var(--type); padding: 6px 12px; cursor: pointer;
  }
  .sheet-close:hover { background: var(--accent); color: #241608; border-color: var(--accent); }
</style>
</head>
<body>
<header>
  <h1 class="brand">Faceplace Marketbook</h1>
  <div class="controls">
    <input type="search" id="q" placeholder="Search title &amp; description…">
    <div class="sel" id="city">
      <button type="button" class="sel-btn" aria-haspopup="listbox" aria-expanded="false">
        <span class="sel-val"></span><span class="caret"></span>
      </button>
      <ul class="sel-list" role="listbox" hidden></ul>
    </div>
    <div class="sel" id="sort">
      <button type="button" class="sel-btn" aria-haspopup="listbox" aria-expanded="false">
        <span class="sel-val"></span><span class="caret"></span>
      </button>
      <ul class="sel-list" role="listbox" hidden></ul>
    </div>
  </div>
</header>
<div class="tally">
  <span id="count"></span>
  <button type="button" id="hiddenToggle" hidden></button>
</div>
<main id="grid"></main>
<div class="scrim" id="scrim" hidden>
  <div class="sheet" role="dialog" aria-modal="true" aria-labelledby="sheetTitle"></div>
</div>
<script id="data" type="application/json">__DATA__</script>
<script>
  const rows = JSON.parse(document.getElementById('data').textContent);
  rows.forEach((r, i) => {
    r._i = i;
    r._price = parseFloat((r.price || '').replace(/[^0-9.]/g, '')) || 0;
    r._hay = ((r.title || '') + ' ' + (r.description || '') + ' ' +
              (r.listing_location || '')).toLowerCase();
  });
  const esc = s => (s || '').replace(/[&<>"]/g,
    m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m]));

  function makeSelect(host, opts, onChange) {
    const btn = host.querySelector('.sel-btn');
    const label = host.querySelector('.sel-val');
    const list = host.querySelector('.sel-list');
    let value = opts[0].value, active = 0;
    label.textContent = opts[0].label;
    const paint = () => {
      list.innerHTML = opts.map((o, i) =>
        `<li role="option" data-i="${i}" aria-selected="${o.value === value}"
             class="${o.value === value ? 'on' : ''}${i === active ? ' active' : ''}"
          >${esc(o.label)}</li>`).join('');
      list.querySelector('.active')?.scrollIntoView({ block: 'nearest' });
    };
    const open = () => {
      active = Math.max(0, opts.findIndex(o => o.value === value));
      paint();
      list.hidden = false;
      btn.setAttribute('aria-expanded', 'true');
    };
    const close = () => {
      list.hidden = true;
      btn.setAttribute('aria-expanded', 'false');
    };
    const pick = i => {
      value = opts[i].value;
      label.textContent = opts[i].label;
      close();
      btn.focus();
      onChange();
    };
    btn.addEventListener('click', () => list.hidden ? open() : close());
    list.addEventListener('click', e => {
      const li = e.target.closest('li');
      if (li) pick(+li.dataset.i);
    });
    host.addEventListener('keydown', e => {
      if (e.key === 'Escape') { close(); return; }
      if (list.hidden) {
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); open(); }
        return;
      }
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        active = Math.min(opts.length - 1,
                          Math.max(0, active + (e.key === 'ArrowDown' ? 1 : -1)));
        paint();
      } else if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        pick(active);
      }
    });
    document.addEventListener('click', e => { if (!host.contains(e.target)) close(); });
    return { get value() { return value; } };
  }

  const cities = [...new Set(rows.map(r => r.location_searched).filter(Boolean))].sort();
  const cityCtl = makeSelect(document.getElementById('city'),
    [{ value: '', label: 'All searched cities' },
     ...cities.map(c => ({ value: c, label: c }))], () => render());
  const sortCtl = makeSelect(document.getElementById('sort'), [
    { value: 'default', label: 'Original order' },
    { value: 'price-asc', label: 'Price: low → high' },
    { value: 'price-desc', label: 'Price: high → low' },
    { value: 'title', label: 'Title A→Z' },
  ], () => render());

  const imgTag = (r, lazy) => r.image
    ? `<img src="${esc(r.image)}"${lazy ? ' loading="lazy"' : ''}
           onerror="this.replaceWith(Object.assign(document.createElement('div'),
                    {className:'noimg',textContent:'image expired'}))">`
    : `<div class="noimg">no image</div>`;

  // Hidden listings are remembered by item_id, so the state survives filtering,
  // sorting, reloads, and even a rebuild of this file from a fresh sweep.
  const HIDE_KEY = 'faceplace-marketbook-hidden';
  let hidden = new Set();
  try {
    hidden = new Set(JSON.parse(localStorage.getItem(HIDE_KEY) || '[]'));
  } catch (e) { /* file:// with storage disabled — keep it in memory only */ }
  const saveHidden = () => {
    try {
      localStorage.setItem(HIDE_KEY, JSON.stringify([...hidden]));
    } catch (e) { /* no-op */ }
  };
  let showHidden = false;

  function card(r) {
    const isHidden = hidden.has(r.item_id);
    const meta = [r.listing_location, r.miles].filter(Boolean).join(' · ');
    return `<div class="card${isHidden ? ' is-hidden' : ''}" data-i="${r._i}" tabindex="0">
      <div class="imgwrap">${imgTag(r, true)}
        <button type="button" class="hide-btn"
                aria-label="${isHidden ? 'Unhide' : 'Hide'} this listing"
                title="${isHidden ? 'Unhide' : 'Hide'} this listing"
        >${isHidden ? 'UNHIDE' : '✕'}</button>
      </div>
      <div class="body">
        <div class="price">${esc(r.price) || '—'}</div>
        <div class="title">${esc(r.title) || '(no title)'}</div>
        <div class="meta">${esc(meta)}</div>
        ${r.description ? `<div class="desc">${esc(r.description)}</div>` : ''}
        <div class="foot"><a href="${esc(r.url)}" target="_blank" rel="noopener">
          View on Facebook ↗</a></div>
      </div>
    </div>`;
  }

  const scrim = document.getElementById('scrim');
  const sheet = scrim.querySelector('.sheet');
  let lastFocus = null;

  function openSheet(r) {
    const meta = [r.listing_location, r.miles].filter(Boolean).join(' · ');
    const found = [r.location_searched && `found in ${r.location_searched} sweep`,
                   r.query && `query “${r.query}”`,
                   (r.scraped_at || '').slice(0, 10)].filter(Boolean).join(' · ');
    sheet.innerHTML = `
      <div class="sheet-img">${imgTag(r, false)}</div>
      <div class="sheet-body">
        <button type="button" class="sheet-close">CLOSE ✕</button>
        <div class="price">${esc(r.price) || '—'}</div>
        <div class="title" id="sheetTitle">${esc(r.title) || '(no title)'}</div>
        <div class="meta">${esc(meta)}</div>
        ${r.description
          ? `<div class="sheet-desc"><span class="lbl">Description</span>${esc(r.description)}</div>`
          : `<div class="sheet-none">No description captured</div>`}
        ${found ? `<div class="meta">${esc(found)}</div>` : ''}
        <div class="foot"><a href="${esc(r.url)}" target="_blank" rel="noopener">
          View on Facebook ↗</a></div>
      </div>`;
    scrim.hidden = false;
    document.body.style.overflow = 'hidden';
    sheet.querySelector('.sheet-close').focus();
  }

  function closeSheet() {
    scrim.hidden = true;
    document.body.style.overflow = '';
    lastFocus?.focus();
  }

  const grid = document.getElementById('grid');
  grid.addEventListener('click', e => {
    const btn = e.target.closest('.hide-btn');
    if (btn) {
      const r = rows[+btn.closest('.card').dataset.i];
      hidden.has(r.item_id) ? hidden.delete(r.item_id) : hidden.add(r.item_id);
      saveHidden();
      render();
      return;
    }
    if (e.target.closest('a')) return;  // let the Facebook link through
    const c = e.target.closest('.card');
    if (c) { lastFocus = c; openSheet(rows[+c.dataset.i]); }
  });
  grid.addEventListener('keydown', e => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const c = e.target.closest('.card');
    if (c && !e.target.closest('a') && !e.target.closest('.hide-btn')) {
      e.preventDefault();
      lastFocus = c;
      openSheet(rows[+c.dataset.i]);
    }
  });

  const hiddenToggle = document.getElementById('hiddenToggle');
  hiddenToggle.addEventListener('click', () => {
    showHidden = !showHidden;
    render();
  });
  scrim.addEventListener('click', e => {
    if (e.target === scrim || e.target.closest('.sheet-close')) closeSheet();
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !scrim.hidden) closeSheet();
  });

  function render() {
    const q = document.getElementById('q').value.trim().toLowerCase();
    const city = cityCtl.value;
    const sort = sortCtl.value;
    let out = rows.filter(r =>
      (!q || r._hay.includes(q)) && (!city || r.location_searched === city));
    const hiddenHere = out.filter(r => hidden.has(r.item_id)).length;
    if (!showHidden) out = out.filter(r => !hidden.has(r.item_id));
    if (sort === 'price-asc') out.sort((a, b) => a._price - b._price);
    else if (sort === 'price-desc') out.sort((a, b) => b._price - a._price);
    else if (sort === 'title') out.sort((a, b) => (a.title || '').localeCompare(b.title || ''));
    else out.sort((a, b) => a._i - b._i);
    const shown = showHidden ? out.length - hiddenHere : out.length;
    document.getElementById('count').textContent =
      shown === rows.length ? `${rows.length} listings`
                            : `${shown} of ${rows.length} listings`;
    hiddenToggle.hidden = hiddenHere === 0;
    hiddenToggle.textContent = showHidden
      ? `hide ${hiddenHere} hidden`
      : `show ${hiddenHere} hidden`;
    document.getElementById('grid').innerHTML = out.length
      ? out.map(card).join('')
      : '<div class="empty">No listings match.</div>';
  }
  document.getElementById('q').addEventListener('input', render);
  render();
</script>
</body>
</html>
"""


def embed_images(rows, base_dir):
    """Rewrite local thumbnail paths to data URIs so the HTML stands alone.
    Stops embedding once EMBED_BUDGET_MB is used up and leaves the rest as
    paths, so a huge multi-city run can't produce an unopenable file."""
    budget = EMBED_BUDGET_MB * 1024 * 1024
    used, done, skipped = 0, 0, 0
    for r in rows:
        img = r.get("image") or ""
        if not img or img.startswith(("http", "data:")):
            continue
        p = Path(img)
        if not p.is_absolute():
            p = base_dir / p
        if not p.exists():
            continue
        size = p.stat().st_size
        if used + size > budget:
            skipped += 1
            continue
        mime = mimetypes.guess_type(p.name)[0] or "image/jpeg"
        r["image"] = (f"data:{mime};base64,"
                      + base64.b64encode(p.read_bytes()).decode("ascii"))
        used += size
        done += 1
    return done, skipped, used


def build(csv_in, out=None, embed=True):
    src = Path(csv_in)
    with open(src, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    seen, uniq = set(), []
    for r in rows:
        iid = r.get("item_id")
        if iid and iid not in seen:
            seen.add(iid)
            uniq.append(r)
    note = ""
    if embed:
        done, skipped, used = embed_images(uniq, src.resolve().parent)
        note = f", {done} images baked in ({used / 1e6:.1f} MB)"
        if skipped:
            note += f"; {skipped} left as file links (over {EMBED_BUDGET_MB} MB budget)"
    # "</" must not appear inside a <script> block; escape it in the JSON.
    data = json.dumps(uniq, ensure_ascii=False).replace("</", "<\\/")
    out = Path(out) if out else src.with_name("gallery.html")
    out.write_text(HTML.replace("__DATA__", data), encoding="utf-8")
    print(f"Wrote {out} ({len(uniq)} listings{note}).")
    if not embed:
        print("  Keep the thumbs/ folder next to the HTML, and open the file "
              "directly in a browser — preview panes often can't resolve "
              "relative image paths.")
    return out


def main():
    ap = argparse.ArgumentParser(description="Build a browsable HTML gallery from a results CSV.")
    ap.add_argument("csv_in", metavar="CSV")
    ap.add_argument("--out", metavar="HTML", help="output path (default: gallery.html next to the CSV)")
    ap.add_argument("--no-embed", action="store_true",
                    help="link thumbnails by path instead of baking them into the HTML")
    a = ap.parse_args()
    build(a.csv_in, a.out, embed=not a.no_embed)


if __name__ == "__main__":
    main()
