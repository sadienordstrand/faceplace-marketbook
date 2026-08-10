const LOCATIONS = __LOCATIONS__;
const BUILTINS = __BUILTINS__;
const PACES = __PACES__;
const DEFAULTS = __DEFAULTS__;
const SAVED = __SAVED__;
const EMAIL = __EMAIL__;
const UNITS = __UNITS__;
const SHORTCUT = __SHORTCUT__;
// The shortcut offer sits over everything, so while it's up it owns the
// keyboard. Declared here because the key handlers below consult it.
let shortcutOpen = false;
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
    // The label is cut off with an ellipsis when it's wider than its tile, so it
    // carries the full name as a tooltip.
    d.innerHTML = `<span class="box">✓</span>`
      + `<span class="lbl" title="${escHtml(label)}">${escHtml(label)}</span>`
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

// One click removes it. Adding it back is a paste of the same Marketplace link,
// so a confirmation step would cost more than the mistake it prevents.
cityWrap.addEventListener('click', async e => {
  const x = e.target.closest('.tog-x');
  if (!x) return;
  e.stopPropagation();
  const label = x.closest('.tog').dataset.city;
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

// The range the year reader itself works in, kept in step with listings.py. A
// bound outside it can only ever match nothing, so the form refuses it rather
// than let a sweep run for an hour and come back empty.
const EARLIEST_YEAR = 1900;
const LATEST_YEAR = new Date().getFullYear() + 1;
['min_year', 'max_year'].forEach(id => {
  $(id).min = EARLIEST_YEAR;
  $(id).max = LATEST_YEAR;
});

function collect() {
  return {
    query: $('query').value.trim(),
    cities: [...cityWrap.querySelectorAll('.tog')]
      .filter(t => t.getAttribute('aria-pressed') === 'true')
      .map(t => t.dataset.city),
    exact: on('exact'),
    min_price: num('min_price'),
    max_price: num('max_price'),
    min_year: num('min_year'),
    max_year: num('max_year'),
    include_no_year: on('include_no_year'),
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

// A range that can't match anything is a mistake worth catching in the form,
// because the sweep it starts looks exactly like a successful one that simply
// found nothing — an hour of scrolling, then an empty gallery.
function filterProblems(c) {
  const out = [];
  if (c.min_price < 0 || c.max_price < 0) out.push("A price can't be negative.");
  if (c.min_price != null && c.max_price != null && c.min_price > c.max_price)
    out.push('The minimum price is higher than the maximum price.');
  const badYear = y => y != null && (y < EARLIEST_YEAR || y > LATEST_YEAR);
  if (badYear(c.min_year) || badYear(c.max_year))
    out.push(`Years have to be between ${EARLIEST_YEAR} and ${LATEST_YEAR}.`);
  if (c.min_year != null && c.max_year != null && c.min_year > c.max_year)
    out.push('The minimum year is later than the maximum year.');
  return out;
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
  // The filters are far enough up the page to be off-screen, so the footer has
  // to say why the button it sits next to went dead.
  const problems = filterProblems(c);
  say('filterMsg', problems.join(' '), 'bad');
  $('est').innerHTML = parts.join(' &middot; ')
    + (c.query ? '' : ' &middot; <span class="warn">query required</span>')
    + (problems.length ? ' &middot; <span class="warn">check the filters</span>' : '');
  $('start').disabled = !c.query || c.cities.length === 0 || problems.length > 0;
  $('saveSearch').disabled = problems.length > 0;
}

$('query').addEventListener('input', refresh);
['min_price','max_price','min_year','max_year','exclude','limit',
 'descriptions_budget'].forEach(id => $(id).addEventListener('input', refresh));

$('start').onclick = () => {
  $('start').disabled = true;
  window.pySubmit({action: 'sweep', ...collect()});
};
$('cancel').onclick = () => window.pyCancel();
document.addEventListener('keydown', e => {
  // Escape has to put the shortcut offer away rather than abandon the window
  // sitting behind it, and Start sweep isn't reachable until it's answered.
  if (shortcutOpen) {
    if (e.key === 'Escape') { e.preventDefault(); $('shortcutSkip').click(); }
    return;
  }
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
  $('min_year').value = s.min_year == null ? '' : s.min_year;
  $('max_year').value = s.max_year == null ? '' : s.max_year;
  $('include_no_year').setAttribute('aria-pressed',
    s.include_no_year === false ? 'false' : 'true');
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
    text.split('\n').forEach((line, i) => {
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
    say('schedMsg', (res.messages || []).join('\n\n'), 'ok');
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
  say('schedMsg', (res.messages || []).join('\n\n'), 'ok');
};

// ------------------------------------------------- the shortcut offer
// Shown by itself only on a launch where there's no shortcut yet and nobody has
// asked to be left alone about it; Python decides that and sends the answer in.
// The button on the Email & schedule tab opens the same sheet on demand.
let shortcutSettled = false;   // true once Python has recorded what they chose
let shortcutReturn = 'query';  // what had focus before the sheet took it

function closeShortcut() {
  shortcutOpen = false;
  $('shortcutAsk').hidden = true;
  $(shortcutReturn).focus();
}

$('shortcutSkip').onclick = async () => {
  const never = !shortcutSettled && on('shortcutNever');
  closeShortcut();
  if (never) await window.pyShortcutNever();
};

$('shortcutAdd').onclick = async () => {
  const wanted = [...$('shortcutPlaces').querySelectorAll('.tog')]
    .filter(t => t.getAttribute('aria-pressed') === 'true')
    .map(t => t.dataset.place);
  if (!wanted.length) { $('shortcutSkip').click(); return; }
  const btn = $('shortcutAdd');
  btn.disabled = true;
  btn.textContent = 'Adding…';
  const res = await window.pyAddShortcut(wanted, on('shortcutNever'));
  btn.disabled = false;
  btn.textContent = 'Add shortcut';
  if (res.error) { say('shortcutMsg', res.error, 'bad'); return; }
  // Done with the question: the only thing left to do is close it. A res.ok of
  // false means some of it landed and some of it didn't, and res.message says
  // which — so it's reported plainly rather than dressed up as a success.
  shortcutSettled = true;
  say('shortcutMsg', res.message, res.ok === false ? '' : 'ok');
  $('shortcutPlaces').hidden = true;
  $('shortcutNote').hidden = true;
  $('shortcutNever').hidden = true;
  btn.hidden = true;
  $('shortcutSkip').textContent = 'Close';
  $('shortcutSkip').className = 'go';
  $('shortcutSkip').focus();
};

// Clicking the darkened area around the panel is the usual way out of one.
$('shortcutAsk').addEventListener('click', e => {
  if (e.target === $('shortcutAsk')) $('shortcutSkip').click();
});

function openShortcut(offer) {
  const o = offer || SHORTCUT;
  // A sheet that's already been through a successful add has its places, its
  // note and two of its buttons put away, so opening one again has to undo all
  // of that rather than show the leftovers of last time.
  shortcutSettled = false;
  $('shortcutMsg').hidden = true;
  $('shortcutPlaces').hidden = false;
  $('shortcutNever').hidden = false;
  $('shortcutNever').setAttribute('aria-pressed', 'false');
  $('shortcutAdd').hidden = false;
  $('shortcutAdd').disabled = false;
  $('shortcutAdd').textContent = 'Add shortcut';
  $('shortcutSkip').textContent = 'Not now';
  $('shortcutSkip').className = 'cancel';
  $('shortcutWhy').textContent = o.why || '';
  $('shortcutNote').textContent = o.note || '';
  $('shortcutNote').hidden = !o.note;
  $('shortcutPlaces').innerHTML = (o.places || []).map(p =>
    `<div class="tog" data-place="${escHtml(p.id)}" role="button" tabindex="0"
          aria-pressed="${p.on ? 'true' : 'false'}">`
    + `<span class="box">✓</span>${escHtml(p.label)}</div>`).join('');
  shortcutOpen = true;
  $('shortcutAsk').hidden = false;
  $('shortcutAdd').focus();
}

// The offer only puts itself in front of someone once. This is the way back to
// it — including after "don't ask again", which is meant to stop the asking
// rather than to rule out ever having one.
$('shortcutOpen').onclick = async () => {
  const btn = $('shortcutOpen');
  btn.disabled = true;
  const res = await window.pyReopenShortcut();
  btn.disabled = false;
  if (res.error) { say('shortcutOpenMsg', res.error, 'bad'); return; }
  if (!res.ask) {
    say('shortcutOpenMsg', "This computer hasn't anywhere to put one.", 'bad');
    return;
  }
  $('shortcutOpenMsg').hidden = true;
  shortcutReturn = 'shortcutOpen';
  openShortcut(res);
};

if (DEFAULTS.query) $('query').value = DEFAULTS.query;
if (DEFAULTS.exclude) $('exclude').value = DEFAULTS.exclude;
showTab('new');
refresh();
$('query').focus();
if (SHORTCUT.ask) openShortcut();
