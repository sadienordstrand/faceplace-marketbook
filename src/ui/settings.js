const LOCATIONS = __LOCATIONS__;
const BUILTINS = __BUILTINS__;
const PACES = __PACES__;
const DEFAULTS = __DEFAULTS__;
const SAVED = __SAVED__;
const EMAIL = __EMAIL__;
const SCHEDULE = __SCHEDULE__;
const UNITS = __UNITS__;
const SHORTCUT = __SHORTCUT__;
// The shortcut offer sits over everything, so while it's up it owns the
// keyboard. Declared here because the key handlers below consult it.
let shortcutOpen = false;
// The two things a scheduled search needs before it is worth saving one, both
// answered before the window opens so the block that says so is right on arrival
// rather than after the first refusal. Declared up here because refresh()
// consults them on every keystroke.
//
// Without email it would run on time, find things, and tell nobody. Without
// automatic runs nothing would start it at all. Either way what gets saved looks
// scheduled and isn't, which is the one outcome worth barring outright.
let emailReady = !!EMAIL.ready;
let schedReady = !!SCHEDULE.ready;
const canSchedule = () => emailReady && schedReady;
// Fixed per-listing costs no pace setting can remove: loading the page and
// reading its payload, plus saving the photo when thumbnails are on.
const PAGE_WORK = DEFAULTS.page_work || 3.5;
const PHOTO_SAVE = DEFAULTS.photo_save || 1.5;

const $ = id => document.getElementById(id);
const secsPer = (p, withThumbs) =>
  PAGE_WORK + (withThumbs ? PHOTO_SAVE : 0) + (PACES[p][0] + PACES[p][1]) / 2;

// Queries. A search can be several of them, OR'd: a listing is kept if it has
// every word of any one. The first box lives in the markup, so it keeps the id
// the rest of the window reaches for; the others are built here.
const MAX_QUERIES = DEFAULTS.max_queries || 5;
const queryList = $('queryList');
const queryBoxes = () => [...queryList.querySelectorAll('.qbox')];

function addQueryBox(value) {
  if (queryBoxes().length >= MAX_QUERIES) return null;
  const wrap = document.createElement('div');
  // The OR and the box it introduces are one element, so removing a query takes
  // its OR with it and never leaves one dangling at the end of the list.
  wrap.className = 'qmore';
  wrap.innerHTML = '<div class="qor">or</div>'
    + '<div class="qrow"><input type="text" class="qbox"'
    + ' placeholder="something else"><button class="qx"'
    + ' title="Remove this query">✕</button></div>';
  queryList.appendChild(wrap);
  const box = wrap.querySelector('.qbox');
  if (value) box.value = value;
  box.addEventListener('input', refresh);
  refresh();
  return box;
}

// Used when a scheduled search is loaded back in, so the boxes match what it holds
// rather than whatever the form was left on.
function setQueries(list) {
  queryList.querySelectorAll('.qmore').forEach(el => el.remove());
  const vals = (list || []).map(q => (q || '').trim()).filter(Boolean);
  $('query').value = vals[0] || '';
  vals.slice(1).forEach(v => addQueryBox(v));
  refresh();
}

$('addQuery').onclick = () => {
  const box = addQueryBox();
  if (box) box.focus();
};

queryList.addEventListener('click', e => {
  const x = e.target.closest('.qx');
  if (!x) return;
  x.closest('.qmore').remove();
  refresh();
});

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

// Adding or removing a city changes a list the user keeps, so it's answered
// with the same green note that saving a search gets. The grey hint above it
// stays put: it's instructions, and an answer written into it reads as more of
// the same rather than as something that just happened.
function sayCity(msg, kind) { say('cityMsg', msg, kind); }

$('addCity').onclick = async () => {
  const btn = $('addCity'), label = $('new_city_label').value.trim();
  const url = $('new_city_url').value.trim();
  btn.disabled = true;
  sayCity('Adding…');
  const res = await window.pyAddCity(label, url);
  btn.disabled = false;
  if (res.error) { sayCity(res.error, 'bad'); return; }
  const keep = selectedCities();
  res.cities.filter(c => !LOCATIONS.includes(c)).forEach(c => keep.add(c));
  LOCATIONS.length = 0; LOCATIONS.push(...res.cities);
  renderCities(res.cities, keep);
  $('new_city_label').value = ''; $('new_city_url').value = '';
  sayCity(`Added ${res.added}.`, 'ok');
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
  sayCity(res.error || `Removed ${label}.`, res.error ? 'bad' : 'ok');
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

// The four boxes filterProblems() has something to say about. Leaving one is
// what puts the number up for judgement, so each has to ask for another look.
const RANGE_BOXES = ['min_price', 'max_price', 'min_year', 'max_year'];
RANGE_BOXES.forEach(id => $(id).addEventListener('blur', refresh));

function collect() {
  return {
    queries: queryBoxes().map(b => b.value.trim()).filter(Boolean),
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
    debug_dump: on('debug_dump'),
    pace: pace,
    limit: num('limit'),
  };
}

// A range that can't match anything is a mistake worth catching in the form,
// because the sweep it starts looks exactly like a successful one that simply
// found nothing — an hour of scrolling, then an empty gallery.
function filterProblems(c) {
  // Whichever box is being typed in sits out the check: "1995" is "1", then
  // "19", then "199" on the way, and a complaint that appears and clears itself
  // between keystrokes is noise. Leaving the box settles the number.
  const typing = document.activeElement ? document.activeElement.id : '';
  const val = id => (id === typing ? null : c[id]);
  const minPrice = val('min_price'), maxPrice = val('max_price');
  const minYear = val('min_year'), maxYear = val('max_year');
  const out = [];
  if (minPrice < 0 || maxPrice < 0) out.push("A price can't be negative.");
  if (minPrice != null && maxPrice != null && minPrice > maxPrice)
    out.push('The minimum price is higher than the maximum price.');
  const badYear = y => y != null && (y < EARLIEST_YEAR || y > LATEST_YEAR);
  if (badYear(minYear) || badYear(maxYear))
    out.push(`Years have to be between ${EARLIEST_YEAR} and ${LATEST_YEAR}.`);
  if (minYear != null && maxYear != null && minYear > maxYear)
    out.push('The minimum year is later than the maximum year.');
  return out;
}

// "$5,000–$40,000", "$5,000 and up", "up to $40,000" — and nothing at all when
// neither end of the range was given. A one-sided year reads differently from a
// one-sided price, so each end's wording comes from the caller.
function rangeText(lo, hi, fmt, low, high) {
  if (lo != null && hi != null) return `${fmt(lo)}–${fmt(hi)}`;
  if (lo != null) return low(fmt(lo));
  if (hi != null) return high(fmt(hi));
  return '';
}

// Everything that changes what this search will do, in the order the form asks
// for it. By the time it's filled in, the cards this describes are several
// screens tall, so the footer is the one place the whole search can be read at
// once — worth more than the seconds-per-listing figure that used to be here,
// which the pace control says itself, right beside the choice.
function summary(c) {
  const bits = [];
  const count = (n, one, many) => `<b>${n}</b> ${n === 1 ? one : many}`;
  if (c.queries.length)
    bits.push(c.queries.map(q => `“${escHtml(q)}”`).join(' or '));
  if (c.exact) bits.push('exact matching');
  bits.push(count(c.cities.length, 'city', 'cities'));
  const price = rangeText(c.min_price, c.max_price, n => '$' + n.toLocaleString(),
                          s => `${s} and up`, s => `up to ${s}`);
  const years = rangeText(c.min_year, c.max_year, String,
                          s => `${s} and later`, s => `${s} and earlier`);
  if (price) bits.push(`<b>${price}</b>`);
  if (years) bits.push(`<b>${years}</b>`);
  if (!c.include_no_year) bits.push('no undated listings');
  const excluded = c.exclude.split(',').filter(t => t.trim()).length;
  if (excluded) bits.push(count(excluded, 'excluded term', 'excluded terms'));
  if (!c.do_descriptions && !c.do_thumbs) bits.push('no descriptions or thumbnails');
  else if (!c.do_descriptions) bits.push('no descriptions');
  else if (!c.do_thumbs) bits.push('no thumbnails');
  if (c.debug_dump) bits.push('save raw payloads');
  if (c.do_descriptions) {
    // No cap is the default, and a summary that recites the defaults back is
    // just noise to read past on the way to what was actually changed.
    if (c.limit) bits.push(count(c.limit, 'description max', 'descriptions max'));
    bits.push(`<b>${pace}</b> retrieval`);
  }
  return bits;
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
  const problems = filterProblems(c);
  say('filterMsg', problems.join(' '), 'bad');
  // Every reason the button can be dead is named here. The setting behind it is
  // usually scrolled off the top of the window by the time anyone reaches for
  // Start Search, so a greyed-out button with nothing beside it reads as a
  // broken one rather than as a step that was missed.
  const warns = [];
  if (!c.queries.length) warns.push('query required');
  if (!c.cities.length) warns.push('select at least one city');
  if (problems.length) warns.push('fix quality filters');
  $('est').innerHTML = summary(c)
    .concat(warns.map(w => `<span class="warn">${w}</span>`))
    .join(' &middot; ');
  $('start').disabled = warns.length > 0;
  $('saveSearch').disabled = problems.length > 0 || !canSchedule();
  $('addQuery').disabled = queryBoxes().length >= MAX_QUERIES;
}

$('query').addEventListener('input', refresh);
['min_price', 'max_price', 'min_year', 'max_year', 'exclude', 'limit']
  .forEach(id => $(id).addEventListener('input', refresh));

// A link here goes to the everyday browser, never to this window: this one is
// Playwright's, so it has no address bar to get back from, isn't logged into
// Facebook, and closes the moment a search starts.
document.addEventListener('click', e => {
  const a = e.target.closest('a[href^="http"]');
  if (!a) return;
  e.preventDefault();
  window.pyOpenLink(a.href);
});

$('start').onclick = () => {
  $('start').disabled = true;
  window.pySubmit({action: 'sweep', ...collect()});
};
$('cancel').onclick = () => window.pyCancel();
document.addEventListener('keydown', e => {
  // Escape has to put the shortcut offer away rather than abandon the window
  // sitting behind it, and Start Search isn't reachable until it's answered.
  if (shortcutOpen) {
    if (e.key === 'Escape') { e.preventDefault(); $('shortcutSkip').click(); }
    return;
  }
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && tab === 'new') {
    // Clicking Start leaves whatever box was being typed in, which is what
    // hands a half-typed price or year over to be checked. This shortcut never
    // leaves it, so it lets go of the box first and reads the answer after.
    if (RANGE_BOXES.includes(document.activeElement.id))
      document.activeElement.blur();
    if (!$('start').disabled) $('start').click();
  }
  if (e.key === 'Escape') window.pyCancel();
});

// ---------------------------------------------------------------- tabs
let tab = 'new';
const PANES = {new: 'paneNew', saved: 'paneSaved', past: 'panePast',
               email: 'paneEmail'};
const TABS = {new: 'tabNew', saved: 'tabSaved', past: 'tabPast',
              email: 'tabEmail'};

function showTab(which) {
  tab = which;
  Object.entries(PANES).forEach(([k, id]) => { $(id).hidden = k !== which; });
  Object.entries(TABS).forEach(([k, id]) =>
    $(id).setAttribute('aria-selected', k === which ? 'true' : 'false'));
  // Start Search only means something on the search tab.
  $('start').hidden = which !== 'new';
  $('est').hidden = which !== 'new';
  $('cancel').textContent = which === 'new' ? 'Cancel' : 'Close';
  if (which === 'saved') renderSaved();
  if (which === 'past') loadRuns();
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
  $('save_unit').appendChild(o);
});
$('save_unit').value = UNITS.includes('days') ? 'days' : UNITS[0];

// Hours are a fixed menu, not a free number: an hourly search runs at set times
// of day (anchored at DAILY_HOUR) so the Mac can be given its wake-ups weeks in
// advance, and only counts that divide 24 land on the same times every day.
const HOUR_CHOICES = SCHEDULE.hour_choices || [3, 4, 6, 8, 12];
const DAILY_HOUR = SCHEDULE.daily_hour || 5;
HOUR_CHOICES.forEach(n => {
  const o = document.createElement('option');
  o.value = String(n);
  o.textContent = String(n);
  $('save_every_hours').appendChild(o);
});

const hour12 = h => h === 0 ? '12 am' : h < 12 ? `${h} am`
  : h === 12 ? '12 pm' : `${h - 12} pm`;

// The times of day an every-N-hours search runs at, spelled out so "every 6
// hours" is never a guess about where the sixes fall.
function gridTimes(every) {
  const out = [];
  for (let k = 0; k < Math.ceil(24 / every); k++)
    out.push((DAILY_HOUR + k * every) % 24);
  return out.sort((a, b) => a - b).map(hour12);
}

// One box holds the count, but it's two controls: a free number for days (and
// dev-mode minutes), a menu for hours. Whichever is hidden is dead weight, so
// the value always comes from the visible one.
function everyValue() {
  return $('save_unit').value === 'hours'
    ? Number($('save_every_hours').value) || HOUR_CHOICES[0]
    : Number($('save_every').value) || 1;
}

function swapEveryBox() {
  const hours = $('save_unit').value === 'hours';
  $('save_every').hidden = hours;
  $('save_every_hours').hidden = !hours;
  const hint = $('everyHint');
  hint.hidden = !hours;
  if (hours) {
    const times = gridTimes(everyValue());
    hint.textContent = 'Runs at the same times every day: '
      + times.slice(0, -1).join(', ')
      + (times.length > 1 ? ' and ' + times[times.length - 1] : times[0]) + '.';
  }
}

// The number and the unit beside it are read as one phrase, so "every 1 days"
// has to become "every 1 day". Only the labels change; the values the scheduler
// is given are always the plural ones it stores.
function labelUnits() {
  const one = everyValue() === 1;
  [...$('save_unit').options].forEach(o => {
    o.textContent = one ? o.value.replace(/s$/, '') : o.value;
  });
}

// A whole count of hours or days is the only thing the scheduler will take, and
// a number box hands over '0', '-1' and '2.5' just as readily, so those are
// corrected as they're typed rather than refused later. A box emptied to be
// retyped is left alone until focus leaves it.
function fixEvery(final) {
  const box = $('save_every');
  if (box.value === '' && !final) return;
  const whole = String(Math.max(1, Math.floor(Number(box.value) || 1)));
  if (whole !== box.value) box.value = whole;
}

// Relabelling happens once the number is settled, not on every keystroke:
// typing "10" passes through "1", and a unit that flicks to "day" and back to
// "days" between two keystrokes reads as a glitch.
$('save_every').addEventListener('input', () => fixEvery());
$('save_every').addEventListener('change', labelUnits);
$('save_every').addEventListener('blur', () => { fixEvery(true); labelUnits(); });
$('save_every_hours').addEventListener('change', swapEveryBox);
$('save_unit').addEventListener('change', () => { swapEveryBox(); labelUnits(); });
labelUnits();
swapEveryBox();

let editingId = null;

function scheduleFields() {
  return {
    name: $('save_name').value.trim(),
    email_to: $('save_email').value.trim(),
    interval: {every: everyValue(), unit: $('save_unit').value},
  };
}

async function checkWarnings() {
  const res = await window.pyCheckSchedule({...scheduleFields(), id: editingId});
  say('saveWarn', (res.warnings || []).join(' '), 'bad');
}
['save_every', 'save_every_hours', 'save_unit'].forEach(id =>
  $(id).addEventListener('change', checkWarnings));

$('saveSearch').onclick = async () => {
  const btn = $('saveSearch');
  btn.disabled = true;
  say('saveMsg', 'Saving…');
  const payload = {...collect(), ...scheduleFields(), id: editingId};
  const res = await window.pySaveSearch(payload);
  btn.disabled = false;
  // Either one can have been taken away since this window opened — email
  // cleared, automatic runs switched off — in which case the refusal is also the
  // news that this block should have been shut.
  if (res.email_ready === false) setEmailReady(false);
  if (res.schedule_ready === false) { schedReady = false; refreshScheduleGate(); }
  if (res.error) { say('saveMsg', res.error, 'bad'); return; }
  say('saveWarn', (res.warnings || []).join(' '), 'bad');
  say('saveMsg', res.message, 'ok');
  SAVED.length = 0; SAVED.push(...(res.searches || []));
  stopEditing();
};

function startEditing(s) {
  editingId = s.id;
  setQueries(s.queries && s.queries.length ? s.queries : [s.query || '']);
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
  // A search saved before this box existed has no address of its own and has
  // been reporting to the account's own address, so that's what it shows.
  $('save_email').value = s.email_to || reportsTo();
  const every = (s.interval && s.interval.every) || 1;
  $('save_unit').value = (s.interval && s.interval.unit) || 'days';
  $('save_every').value = every;
  // A count the menu doesn't offer (hand-edited json, or saved before hours
  // became a menu) falls back to the first choice rather than leaving the
  // menu with nothing selected.
  if ($('save_unit').value === 'hours') {
    $('save_every_hours').value =
      HOUR_CHOICES.includes(every) ? String(every) : String(HOUR_CHOICES[0]);
  }
  swapEveryBox();
  labelUnits();
  const keep = new Set(s.cities || []);
  cityWrap.querySelectorAll('.tog').forEach(t =>
    t.setAttribute('aria-pressed', keep.has(t.dataset.city) ? 'true' : 'false'));
  $('saveSearch').textContent = 'Update scheduled search';
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

// ------------------------------------------------------- the scheduled searches list
function renderSaved() {
  const wrap = $('savedList');
  if (!SAVED.length) {
    // The box above already says what's missing when something is, so this only
    // has to point somewhere — and not at the New Search tab, where saving is
    // still barred.
    wrap.innerHTML = '<div class="empty">No scheduled searches yet. '
      + (canSchedule() ? 'Set one up at the bottom of the New Search tab.'
                       : 'Finish the setup on the Email & Setup tab to create one.')
      + '</div>';
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
        ${((s.queries && s.queries.length ? s.queries : [s.query || ''])
            .map(q => `“${escHtml(q)}”`).join(' or '))} across ${
          (s.cities || []).length} ${
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
    // Pausing or resuming an hourly search rewrites the Mac's wake-ups, and
    // the note is how a declined password prompt gets reported.
    say('savedMsg', `“${s.name}” is now ${s.enabled ? 'paused' : 'active'}.`
        + (res.note ? ' ' + res.note : ''), 'ok');
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
    say('savedMsg', `Deleted “${s.name}”. Its results are still available in the Past Searches tab.`
        + (res.note ? ' ' + res.note : ''), 'ok');
  }
});

// -------------------------------------------------------------- email settings
function fillMail(cfg) {
  $('mail_address').value = cfg.address || '';
  $('mail_password').value = cfg.app_password || '';
  $('mail_provider').value = cfg.provider || 'gmail';
  $('mail_host').value = cfg.host || '';
  $('mail_port').value = cfg.port || '';
  mailHostRow();
}
function mailHostRow() {
  $('mailHostRow').hidden = $('mail_provider').value !== 'other';
}
$('mail_provider').addEventListener('change', mailHostRow);

// Where a report would go unless a search says otherwise: the account sending
// it. Read off the box, which starts out holding whatever was last saved.
function reportsTo() {
  return $('mail_address').value.trim();
}

// The address a new scheduled search will report to, written into the box rather
// than left blank under a note explaining what a blank would mean. Only ever
// filled when there's nothing there, so an address typed by hand — or one
// loaded from a search being edited — is never overwritten.
function fillReportTo() {
  if (!$('save_email').value.trim()) $('save_email').value = reportsTo();
}

// Why a scheduled search can't be set up yet, or '' when it can. Both reasons
// are named together when both apply: sending someone to fix one and then meeting
// them with the other is how a two-step setup comes to feel like a fault.
function scheduleBlock() {
  if (canSchedule()) return '';
  if (!emailReady && !schedReady) {
    return 'Configure your email settings and turn on automatic runs to enable scheduled searches.';
  }
  if (!emailReady) {
    return 'Configure your email settings to run a scheduled search.';
  }
  // Deliberately not "turned off": on and blocked lands here too, and the
  // instructions for that are already on the tab this sends you to.
  return 'Turn on automatic runs to set up a scheduled search.';
}

// Everywhere the answer shows: the block on the search tab, the one on the
// scheduled searches tab, and whether the fields between them can be touched at
// all. Called again whenever either half of the answer changes.
function refreshScheduleGate() {
  const why = scheduleBlock();
  $('saveBlocked').hidden = !why;
  $('saveBlockedWhy').textContent = why;
  $('savedBlocked').hidden = !why;
  $('savedBlockedWhy').textContent = why;
  $('saveFields').classList.toggle('gated', !!why);
  $('saveFields').querySelectorAll('input, select').forEach(el => {
    el.disabled = !!why;
  });
  // The empty-list text names the same answer, so it moves with it.
  if (tab === 'saved') renderSaved();
  refresh();
}

// Everything that changes when email starts or stops working: the status line
// here, and half of whether a scheduled search can be set up at all.
function setEmailReady(ready) {
  emailReady = !!ready;
  // Where a report lands is each search's own business, so this says where one
  // would come from, which is the part this tab actually decides.
  const from = $('mail_address').value.trim();
  $('mailDot').className = 'dot' + (emailReady ? ' on' : '');
  $('mailState').textContent = emailReady
    ? `Email is set up${from ? ', sending from ' + from : ''}`
    : 'Email isn\'t set up yet';
  fillReportTo();
  refreshScheduleGate();
}

$('goEmail').onclick = () => {
  showTab('email');
  // Whichever of the two is missing is the one worth landing on. Both missing
  // starts at email, which is the one that takes typing.
  if (!emailReady) {
    $('mail_address').focus();
  } else {
    $('schedOn').focus();
    $('schedOn').scrollIntoView({block: 'center'});
  }
};

$('saveMail').onclick = async () => {
  const res = await window.pySaveEmail({
    address: $('mail_address').value.trim(),
    app_password: $('mail_password').value.trim(),
    provider: $('mail_provider').value,
    host: $('mail_host').value.trim(),
    port: Number($('mail_port').value) || 587,
  });
  // A refusal that never reached the disk leaves the state alone; anything
  // that was written reports what it left behind.
  if (res.ready !== undefined) setEmailReady(res.ready);
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

fillMail(EMAIL);
setEmailReady(EMAIL.ready);

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
  // Turning these on or off is the other half of what decides whether a
  // scheduled search can be set up, so the blocks that say so move with it.
  schedReady = !!res.ready;
  showWakes(res.wakes);
  showSystemSettings(res.os, res.hide_settings);
  refreshScheduleGate();
}

// The system settings for this computer, and nothing about the other one:
// its menus don't exist here, and a page that lists both leaves you working
// out which half to ignore. Anywhere that isn't macOS or Windows has no
// automatic runs to make possible, so the section stays away entirely.
// `hide` is the cards already matching what we recommend, so they don't sit
// on the page as a to-do that's already done.
const OS_NAMES = {darwin: 'macOS', windows: 'Windows'};
function showSystemSettings(os, hide) {
  hide = hide || [];
  $('sysMac').hidden = os !== 'darwin';
  $('sysWin').hidden = os !== 'windows';
  $('sysOs').textContent = OS_NAMES[os] || '';
  document.querySelectorAll('[data-sys]').forEach(el => {
    el.hidden = hide.indexOf(el.dataset.sys) >= 0;
  });
  // A Recommended / Optional heading with nothing under it is worse than none.
  document.querySelectorAll('.sysrule').forEach(rule => {
    let el = rule.nextElementSibling, any = false;
    while (el && !el.classList.contains('sysrule')) {
      if (el.classList.contains('setting') && !el.hidden) any = true;
      el = el.nextElementSibling;
    }
    rule.hidden = !any;
  });
  const grp = os === 'darwin' ? $('sysMac') : os === 'windows' ? $('sysWin') : null;
  const any = grp && [...grp.querySelectorAll('.setting')].some(el => !el.hidden);
  $('sysBlock').hidden = !any;
}
showSystemSettings(SCHEDULE.os, SCHEDULE.hide_settings);

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

// ---------------------------------------------------------- the wake queue
// The one-off wake-ups that get a sleeping Mac up for hourly searches. They're
// written weeks ahead and run down day by day, so the tab shows how far they
// reach, and a Renew button rewrites them — that's the click that can ask for
// the password. Irrelevant on Windows (the scheduled task wakes the machine
// itself) and with no hourly searches, and invisible both times.
function showWakes(wakes) {
  const on = !!(wakes && wakes.relevant);
  $('wakeRow').hidden = !on;
  $('wakeHint').hidden = !on;
  if (!on) return;
  const low = !!wakes.renew;
  const until = wakes.until ? new Date(wakes.until) : null;
  $('wakeDot').className = 'dot' + (low ? ' stuck' : ' on');
  $('wakeState').textContent = !wakes.queued
    ? 'Wake-ups for scheduled searches have run out'
    : `Wake-ups for scheduled searches are in effect until ${
        until.toLocaleDateString(undefined,
        {weekday: 'long', month: 'long', day: 'numeric'})}`
      + (low ? ` — ${wakes.days_left} day${wakes.days_left === 1 ? '' : 's'} left` : '');
  $('wakeHint').textContent = 'A Mac can only schedule its wake-ups a few '
    + 'weeks at a time, so they need renewing now and then. If they expire, '
    + 'searches that are scheduled to run multiple times a day will still run '
    + 'every day at ' + hour12(DAILY_HOUR) + ', and whenever the Mac is awake.';
}

async function renewWakes(msgId) {
  const res = await window.pyRenewWakes();
  say(msgId, res.error || res.message, res.error ? 'bad' : 'ok');
  if (res.wakes) showWakes(res.wakes);
  return !res.error;
}
$('renewWakes').onclick = () => renewWakes('wakeMsg');

// The same fact raised at the front door when it's nearly urgent: below the
// renew threshold the banner is the first thing the window shows, wherever it
// opens. "Not now" costs nothing — the queue keeps counting down, the reports
// start nagging near the end, and the banner returns next launch.
function wakeBanner(wakes) {
  if (!wakes || !wakes.relevant || !wakes.renew) return;
  $('wakeText').textContent = 'You have a scheduled search that runs multiple '
    + 'times a day, which requires periodic renewal so it can keep waking up '
    + 'your computer on schedule. '
    + (wakes.queued
      ? `The current wake-ups run out in ${wakes.days_left} `
        + `day${wakes.days_left === 1 ? '' : 's'}`
      : 'They\'ve already run out')
    + ' — renewing takes one click and your password.';
  $('wakeBar').hidden = false;
}
$('wakeLater').onclick = () => { $('wakeBar').hidden = true; };
$('wakeRenew').onclick = async () => {
  if (await renewWakes('wakeBarMsg')) {
    setTimeout(() => { $('wakeBar').hidden = true; }, 2500);
  }
};
showWakes(SCHEDULE.wakes);
wakeBanner(SCHEDULE.wakes);

// ------------------------------------------------------------ past searches
// What's in runs/, as one card per finished search. Fetched the first time the
// tab is opened rather than at startup: it means reading every run's manifest,
// and most launches never look at this tab at all.
const RUNS = [];
let runsLoaded = false;

function runDetail(r) {
  const bits = [];
  const n = r.listings;
  bits.push(n == null ? 'results on disk'
            : `<b>${n}</b> ${n === 1 ? 'listing' : 'listings'}`);
  if (r.new_listings != null) bits.push(`<b>${r.new_listings}</b> new that run`);
  if (r.cities) bits.push(`${r.cities} ${r.cities === 1 ? 'city' : 'cities'}`);
  if (r.duration_text) bits.push(`took ${r.duration_text}`);
  if (r.earlier_runs) bits.push(`${r.earlier_runs} earlier `
    + `${r.earlier_runs === 1 ? 'run' : 'runs'} kept`);
  return bits.join(' &middot; ');
}

function renderRuns() {
  const wrap = $('runList');
  if (!RUNS.length) {
    wrap.innerHTML = '<div class="empty">Nothing has finished yet. Every '
      + 'search you run leaves its results here.</div>';
    return;
  }
  wrap.innerHTML = RUNS.map(r => `
    <div class="card run" data-id="${escHtml(r.id)}" role="button" tabindex="0">
      <div class="top">
        <span class="nm">${escHtml(r.name)}</span>
        ${r.scheduled ? '<span class="pill">scheduled</span>' : ''}
      </div>
      <div class="det">
        Ran ${escHtml(r.when_text)}<br>
        ${runDetail(r)}
      </div>
      <div class="foot">
        <span class="opens">Open the gallery &rarr;</span>
        <div class="acts"><button class="mini" data-act="del">Delete</button></div>
      </div>
    </div>`).join('');
}

async function loadRuns(force) {
  if (runsLoaded && !force) return;
  const wrap = $('runList');
  wrap.innerHTML = '<div class="empty">Reading the runs folder…</div>';
  const res = await window.pyListRuns();
  runsLoaded = true;
  if (res.error) {
    wrap.innerHTML = '';
    say('runMsg', res.error, 'bad');
    return;
  }
  say('runMsg', '');
  RUNS.length = 0; RUNS.push(...(res.runs || []));
  renderRuns();
}
$('refreshRuns').onclick = () => loadRuns(true);

// The gallery opens in the everyday browser, not in this window: this one is
// the settings window, and it closes as soon as a search starts.
async function openRun(card) {
  const r = RUNS.find(x => x.id === card.dataset.id);
  card.classList.add('busy');
  say('runMsg', `Opening ${r ? r.name : 'that search'}…`);
  const res = await window.pyOpenRun(card.dataset.id);
  card.classList.remove('busy');
  // A gallery that opened is now in front of them, in another window, saying so
  // itself. Only a failure is worth words, because that's the case where
  // nothing visible happened.
  say('runMsg', res.error || '', res.error ? 'bad' : '');
}

// Deleting is permanent, so the button asks for a second click, the same way
// the scheduled searches list does.
function disarmRunDeletes() {
  $('runList').querySelectorAll('button[data-confirm]').forEach(o => {
    delete o.dataset.confirm; o.textContent = 'Delete';
  });
}

async function deleteRun(btn) {
  const card = btn.closest('.card.run');
  const r = RUNS.find(x => x.id === card.dataset.id);
  if (!btn.dataset.confirm) {
    disarmRunDeletes();
    btn.dataset.confirm = '1';
    btn.textContent = 'Really delete?';
    return;
  }
  card.classList.add('busy');
  const res = await window.pyDeleteRun(card.dataset.id);
  if (res.error) {
    card.classList.remove('busy');
    say('runMsg', res.error, 'bad');
    return;
  }
  RUNS.length = 0; RUNS.push(...(res.runs || []));
  renderRuns();
  say('runMsg', `Deleted “${r ? r.name : 'that search'}”.`, 'ok');
}

$('runList').addEventListener('click', e => {
  // The delete button sits inside a card that is itself one big button, so it
  // has to be looked for first or every delete would also open a gallery.
  const btn = e.target.closest('button[data-act]');
  if (btn) { deleteRun(btn); return; }
  const card = e.target.closest('.card.run');
  if (!card) return;
  disarmRunDeletes();
  openRun(card);
});
$('runList').addEventListener('keydown', e => {
  if (e.key !== 'Enter' && e.key !== ' ') return;
  // A button answers those keys itself, with a click this listener has already
  // handled; the card only wants them when it's the card that's focused.
  if (e.target.closest('button')) return;
  const card = e.target.closest('.card.run');
  if (card) { e.preventDefault(); disarmRunDeletes(); openRun(card); }
});

// ------------------------------------------------- the shortcut panel
// Put up by itself on a launch where there's no shortcut yet and nobody has
// asked to be left alone about it; Python decides that and sends the answer in.
// The same panel is reachable any time from the Email & Setup tab, which is the
// way back for someone who said "not now" and changed their mind, or who wants a
// second copy of the icon somewhere else.
let shortcutSettled = false;   // true once Python has recorded what they chose
let shortcutUnprompted = false;  // the launch offer, rather than the button

function closeShortcut() {
  shortcutOpen = false;
  $('shortcutAsk').hidden = true;
  // Back to whatever asked for it: the form the offer was covering, or the
  // button that opened it.
  $(shortcutUnprompted ? 'query' : 'shortcutOpen').focus();
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

// `unprompted` is the launch offer; without it this is the button on the Email &
// Setup tab. Everything the panel does to itself on the way out is undone here
// rather than on close, so opening it a second time starts from the top.
function openShortcut(unprompted) {
  shortcutUnprompted = !!unprompted;
  shortcutSettled = false;
  say('shortcutMsg', '');
  $('shortcutTitle').textContent = unprompted ? 'Add a shortcut?'
                                              : 'Add a shortcut';
  $('shortcutWhy').textContent = SHORTCUT.why || '';
  $('shortcutNote').textContent = SHORTCUT.note || '';
  $('shortcutNote').hidden = !SHORTCUT.note;
  $('shortcutPlaces').hidden = false;
  $('shortcutPlaces').innerHTML = (SHORTCUT.places || []).map(p =>
    `<div class="tog" data-place="${escHtml(p.id)}" role="button" tabindex="0"
          aria-pressed="${p.on ? 'true' : 'false'}">`
    + `<span class="box">✓</span>${escHtml(p.label)}</div>`).join('');
  // "Don't ask again" is only an answer to being asked.
  $('shortcutNever').hidden = !unprompted;
  $('shortcutNever').setAttribute('aria-pressed', 'false');
  $('shortcutAdd').hidden = false;
  $('shortcutAdd').disabled = false;
  // "Not now" answers a question. Nobody asked one when the button was clicked.
  $('shortcutSkip').textContent = unprompted ? 'Not now' : 'Cancel';
  $('shortcutSkip').className = 'cancel';
  shortcutOpen = true;
  $('shortcutAsk').hidden = false;
  $('shortcutAdd').focus();
}

// Only on a computer this app knows how to make a shortcut on.
$('shortcutBlock').hidden = !(SHORTCUT.places || []).length;
$('shortcutOpen').onclick = () => openShortcut();

setQueries(DEFAULTS.queries || (DEFAULTS.query ? [DEFAULTS.query] : []));
if (DEFAULTS.exclude) $('exclude').value = DEFAULTS.exclude;
showTab('new');
refresh();
$('query').focus();
if (SHORTCUT.ask) openShortcut(true);
