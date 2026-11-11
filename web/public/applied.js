/* "My applications": a job-application tracker that lives only in this browser.
 *
 * Nothing here talks to the server. The list is kept in localStorage; moving it
 * to another browser is export-a-CSV, import-the-CSV, and an import MERGES:
 * the same job on both sides keeps whichever copy was changed most recently,
 * jobs on only one side are kept. Deletions do not travel - a job removed here
 * comes back if an older export that still has it is imported.
 *
 * Jobs are keyed so the same listing lines up across browsers: scraped
 * listings by country + listing id, hand-added ones by their URL (or a random
 * key when they have none).
 */

const STORE = 'doomers:applications:v1';
const MAX_ITEMS = 5000;

export const STATUSES = [
  ['applied', 'Applied'], ['oa', 'OA'], ['interview', 'Interview'],
  ['rejected', 'Rejected'], ['accepted', 'Accepted'],
];
const LABEL = Object.fromEntries(STATUSES);
const BY_LABEL = Object.fromEntries(STATUSES.map(([k, l]) => [l.toLowerCase(), k]));
BY_LABEL['online assessment'] = 'oa';
const ROUNDED = new Set(['oa', 'interview']);   // statuses that carry a round + schedule

const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const today = () => new Date().toLocaleDateString('en-CA');   // YYYY-MM-DD, local

let items = new Map();     // key -> item
let storageOk = true;
let filter = '';           // '' = all, else a status
let lastRemoved = null;
const seen = new Map();    // key -> listing row, for "Mark applied" on rendered rows
let onEvent = () => {};

/* ── storage ───────────────────────────────────────────────────────────── */
function clean(o) {
  if (!o || typeof o !== 'object') return null;
  const s = (v, n = 300) => String(v ?? '').trim().slice(0, n);
  const key = s(o.key, 600);
  const title = s(o.title);
  if (!key || !title) return null;
  const st = s(o.status, 40);
  const status = LABEL[st] ? st : BY_LABEL[st.toLowerCase()] || 'applied';
  const round = Number.parseInt(o.round, 10);
  const url = s(o.url, 2000);
  const sched = s(o.scheduled, 16);
  const applied = s(o.applied, 10);
  const upd = Number(o.updated);
  return {
    key, title,
    company: s(o.company), location: s(o.location), site: s(o.site, 40), country: s(o.country, 40),
    url: /^https?:\/\//i.test(url) ? url : '',          // never a javascript: link from an import
    status,
    round: round >= 1 && round <= 20 ? round : null,
    scheduled: /^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2})?$/.test(sched) ? sched : '',
    applied: /^\d{4}-\d{2}-\d{2}$/.test(applied) ? applied : today(),
    updated: Number.isFinite(upd) && upd > 0 ? upd : Date.parse(o.updated) || 0,
  };
}

function load() {
  let raw = null;
  try {
    raw = localStorage.getItem(STORE);
  } catch {
    storageOk = false;
    return;
  }
  try {
    const list = raw ? JSON.parse(raw).items : [];
    items = new Map(list.map(clean).filter(Boolean).map((i) => [i.key, i]));
  } catch {
    // Unreadable: keep a copy rather than silently overwriting it on next save.
    try { localStorage.setItem(`${STORE}:unreadable`, raw); } catch { /* nothing more to do */ }
    items = new Map();
  }
}

function save({ render = true } = {}) {
  try {
    localStorage.setItem(STORE, JSON.stringify({ v: 1, items: [...items.values()] }));
    storageOk = true;
  } catch {
    storageOk = false;
  }
  if (render) renderAll();
  else $('app-storage').hidden = storageOk;
}

/* ── listing rows: the "Mark applied" button ───────────────────────────── */
const listingKey = (r, country) => `j:${country}:${r.id}`;

function markLabel(it) {
  return it ? `✓ ${LABEL[it.status]}` : 'Mark applied';
}

/** HTML for the button on a listing row. Remembers the row so a click can save it. */
export function markButton(r, country) {
  if (!r?.id || !r.title) return '';
  const key = listingKey(r, country);
  seen.set(key, { ...r, country });
  const it = items.get(key);
  return `<button type="button" class="mark${it ? ' on' : ''}" data-mark="${esc(key)}"
    aria-label="${it ? `Tracked as ${LABEL[it.status]} - show in My applications` : `Mark ${esc(r.title)} as applied`}">${markLabel(it)}</button>`;
}

function syncMarks() {
  document.querySelectorAll('button.mark[data-mark]').forEach((b) => {
    const it = items.get(b.dataset.mark);
    b.classList.toggle('on', !!it);
    b.textContent = markLabel(it);
  });
}

function addFromListing(key) {
  const r = seen.get(key);
  if (!r) return;
  items.set(key, clean({
    key, title: r.title, company: r.company, location: r.location, site: r.site,
    country: r.country, url: r.job_url, status: 'applied', applied: today(), updated: Date.now(),
  }));
  save();
  onEvent('app-mark', 'Marked applied');
}

/* ── tabs ──────────────────────────────────────────────────────────────── */
const TABS = ['listings', 'applied'];

function showTab(name, { focus = false } = {}) {
  for (const t of TABS) {
    const btn = $(`tab-btn-${t}`);
    const on = t === name;
    btn.setAttribute('aria-selected', String(on));
    btn.tabIndex = on ? 0 : -1;
    $(`tab-${t}`).hidden = !on;
    if (on && focus) btn.focus();
  }
  const u = new URL(location);
  u.hash = name === 'applied' ? 'applications' : '';
  history.replaceState(null, '', u);
}

/* ── the applications table ────────────────────────────────────────────── */
function sorted() {
  // Applied date, newest first - stable while editing, so a row does not jump
  // out from under the pointer when its status changes.
  return [...items.values()].sort((a, b) =>
    b.applied.localeCompare(a.applied) || a.title.localeCompare(b.title));
}

function renderFilter() {
  const counts = {};
  for (const it of items.values()) counts[it.status] = (counts[it.status] || 0) + 1;
  const chip = (k, label, n) => `<button type="button" class="chip${k ? ` st-${k}` : ''}"
    data-filter="${k}" aria-pressed="${filter === k}">${label} <span>${n}</span></button>`;
  $('app-filter').innerHTML = chip('', 'All', items.size)
    + STATUSES.map(([k, l]) => chip(k, l, counts[k] || 0)).join('');
}

function rowHtml(it) {
  const name = esc(it.title);
  const rounded = ROUNDED.has(it.status);
  const opts = STATUSES.map(([k, l]) =>
    `<option value="${k}"${k === it.status ? ' selected' : ''}>${l}</option>`).join('');
  return `<tr data-key="${esc(it.key)}">
    <td class="title" data-l="Role">${name}</td>
    <td class="co" data-l="Company">${esc(it.company) || '—'}</td>
    <td data-l="Status"><select class="st st-${it.status}" data-f="status" aria-label="Status of ${name}">${opts}</select></td>
    <td data-l="Round"${rounded ? '' : ' class="na"'}>${rounded
      ? `<input type="number" data-f="round" min="1" max="20" inputmode="numeric" value="${it.round ?? ''}" aria-label="Round for ${name}">`
      : '—'}</td>
    <td data-l="Scheduled"${rounded ? '' : ' class="na"'}>${rounded
      ? `<input type="datetime-local" data-f="scheduled" value="${esc(it.scheduled.length === 10 ? `${it.scheduled}T09:00` : it.scheduled)}" aria-label="${LABEL[it.status]} date for ${name}">`
      : '—'}</td>
    <td data-l="Applied"><input type="date" data-f="applied" value="${esc(it.applied)}" aria-label="Date applied to ${name}"></td>
    <td class="actions">${it.url ? `<a href="${esc(it.url)}" target="_blank" rel="noopener noreferrer nofollow">Open&nbsp;↗</a>` : ''}
      <button type="button" class="linkish" data-act="remove" aria-label="Remove ${name}">Remove</button></td>
  </tr>`;
}

function renderTable() {
  const tb = $('apps').querySelector('tbody');
  const rows = sorted().filter((it) => !filter || it.status === filter);
  tb.innerHTML = rows.length
    ? rows.map(rowHtml).join('')
    : `<tr><td colspan="7" class="empty">${items.size
      ? 'No jobs with this status.'
      : 'Nothing tracked yet. Press <b>Mark applied</b> on any listing above, or add a job yourself.'}</td></tr>`;
}

function renderAll() {
  $('app-count').textContent = items.size ? String(items.size) : '';
  $('app-export').disabled = items.size === 0;
  $('app-storage').hidden = storageOk;
  renderFilter();
  renderTable();
  syncMarks();
}

function say(html) {
  $('app-status').innerHTML = html;
}

/* ── CSV ───────────────────────────────────────────────────────────────── */
const COLS = [
  ['title', 'Title'], ['company', 'Company'], ['status', 'Status'], ['round', 'Round'],
  ['scheduled', 'Scheduled'], ['applied', 'Applied on'], ['location', 'Location'],
  ['site', 'Source'], ['url', 'URL'], ['country', 'Country'], ['updated', 'Updated'], ['key', 'Key'],
];
const norm = (h) => h.toLowerCase().replace(/[^a-z]/g, '');
const HEAD_TO_FIELD = Object.fromEntries(COLS.flatMap(([f, h]) => [[norm(h), f], [norm(f), f]]));

function cell(v) {
  let s = String(v ?? '');
  // Spreadsheet apps run cells that start like a formula; the leading quote
  // keeps them text, and import strips it again.
  if (/^[=+\-@\t\r]/.test(s)) s = `'${s}`;
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function toCsv() {
  const lines = [COLS.map(([, h]) => h).join(',')];
  for (const it of sorted()) {
    lines.push(COLS.map(([f]) => cell(
      f === 'status' ? LABEL[it.status]
        : f === 'updated' ? (it.updated ? new Date(it.updated).toISOString() : '')
          : it[f])).join(','));
  }
  return `﻿${lines.join('\r\n')}\r\n`;   // BOM so Excel reads it as UTF-8
}

function parseCsv(text) {
  const rows = [];
  let row = [], f = '', q = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (q) {
      if (c !== '"') f += c;
      else if (text[i + 1] === '"') { f += '"'; i++; } else q = false;
    } else if (c === '"') q = true;
    else if (c === ',') { row.push(f); f = ''; }
    else if (c === '\n' || c === '\r') {
      if (c === '\r' && text[i + 1] === '\n') i++;
      row.push(f); rows.push(row); row = []; f = '';
    } else f += c;
  }
  if (f || row.length) { row.push(f); rows.push(row); }
  return rows.filter((r) => r.some((x) => x.trim()));
}

function exportCsv() {
  const blob = new Blob([toCsv()], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `my-applications-${today()}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  say(`Exported ${items.size} job${items.size === 1 ? '' : 's'}.`);
  onEvent('app-export', 'Exported applications');
}

async function importCsv(file) {
  if (file.size > 5_000_000) { say('That file is too large to be an export from here.'); return; }
  const rows = parseCsv((await file.text()).replace(/^﻿/, ''));
  const fields = (rows.shift() || []).map((h) => HEAD_TO_FIELD[norm(h)]);
  if (!fields.includes('title')) {
    say('That file has no <b>Title</b> column. Import a CSV exported from this page.');
    return;
  }
  let added = 0, updated = 0, same = 0, bad = 0;
  for (const r of rows) {
    const o = {};
    fields.forEach((f, i) => { if (f) o[f] = (r[i] ?? '').replace(/^'(?=[=+\-@\t\r])/, ''); });
    // A row typed in by hand has no key; key it the way "Add a job" would.
    o.key ||= o.url ? `u:${o.url.trim()}` : `m:${norm(`${o.title}${o.company}`)}`;
    const it = clean(o);
    if (!it) { bad++; continue; }
    const have = items.get(it.key);
    if (!have) {
      if (items.size >= MAX_ITEMS) { bad++; continue; }
      items.set(it.key, it); added++;
    } else if (it.updated > have.updated) {
      items.set(it.key, it); updated++;
    } else same++;
  }
  save();
  const parts = [`${added} new`, `${updated} updated`, `${same} already up to date`];
  if (bad) parts.push(`${bad} skipped`);
  say(`Imported: ${parts.join(', ')}.`);
  onEvent('app-import', 'Imported applications');
}

/* ── editing ───────────────────────────────────────────────────────────── */
function edit(key, field, value) {
  const it = items.get(key);
  if (!it) return;
  const next = { ...it, [field]: value, updated: Date.now() };
  if (field === 'status' && ROUNDED.has(value) && value !== it.status) {
    next.round = 1;          // a new stage starts at round 1
    next.scheduled = '';
  }
  items.set(key, clean(next));
  // Only a status change alters the row's shape; re-rendering for a round or a
  // date would pull focus out of the field mid-edit.
  save({ render: field === 'status' });
}

function wire() {
  // "Mark applied" on listing and match rows.
  document.addEventListener('click', (e) => {
    const b = e.target.closest('button.mark[data-mark]');
    if (!b) return;
    const key = b.dataset.mark;
    if (!items.has(key)) { addFromListing(key); return; }
    filter = '';
    renderAll();
    showTab('applied');
    $('panel-listings').scrollIntoView({ behavior: 'smooth', block: 'start' });
    const row = $('apps').querySelector(`tr[data-key="${CSS.escape(key)}"]`);
    row?.querySelector('select')?.focus({ preventScroll: true });
  });

  // Tabs, with arrow keys between them.
  for (const t of TABS) $(`tab-btn-${t}`).addEventListener('click', () => showTab(t));
  $('panel-listings').querySelector('[role=tablist]').addEventListener('keydown', (e) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return;
    const cur = TABS.findIndex((t) => $(`tab-btn-${t}`).getAttribute('aria-selected') === 'true');
    const i = e.key === 'Home' ? 0 : e.key === 'End' ? TABS.length - 1
      : (cur + (e.key === 'ArrowRight' ? 1 : -1) + TABS.length) % TABS.length;
    showTab(TABS[i], { focus: true });
    e.preventDefault();
  });

  $('app-filter').addEventListener('click', (e) => {
    const b = e.target.closest('[data-filter]');
    if (!b) return;
    filter = b.dataset.filter === filter ? '' : b.dataset.filter;
    renderFilter();
    renderTable();
  });

  const tb = $('apps').querySelector('tbody');
  tb.addEventListener('change', (e) => {
    const f = e.target.dataset.f;
    const key = e.target.closest('tr')?.dataset.key;
    if (!f || !key) return;
    edit(key, f, e.target.value);
    if (f === 'status') {
      onEvent(`app-status-${e.target.value}`, 'Application status');
      const sel = tb.querySelector(`tr[data-key="${CSS.escape(key)}"] select`);
      if (sel) sel.focus();
      else say(`Moved to <b>${LABEL[e.target.value]}</b>.`);   // filtered out of view
    }
  });
  tb.addEventListener('click', (e) => {
    const b = e.target.closest('[data-act="remove"]');
    if (!b) return;
    const key = b.closest('tr').dataset.key;
    lastRemoved = items.get(key);
    items.delete(key);
    save();
    say(`Removed <b>${esc(lastRemoved.title)}</b>. <button type="button" class="linkish" id="app-undo">Undo</button>`);
  });
  $('app-status').addEventListener('click', (e) => {
    if (e.target.id !== 'app-undo' || !lastRemoved) return;
    items.set(lastRemoved.key, lastRemoved);
    lastRemoved = null;
    save();
    say('Restored.');
  });

  $('app-add-toggle').addEventListener('click', () => {
    const f = $('app-add');
    f.hidden = !f.hidden;
    $('app-add-toggle').setAttribute('aria-expanded', String(!f.hidden));
    if (!f.hidden) f.elements.title.focus();
  });
  $('app-add').addEventListener('submit', (e) => {
    e.preventDefault();
    const f = e.target.elements;
    const url = f.url.value.trim();
    if (url && !/^https?:\/\//i.test(url)) { say('The link has to start with http:// or https://'); return; }
    const key = url ? `u:${url}` : `m:${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
    const it = clean({ key, title: f.title.value, company: f.company.value, url,
      status: 'applied', applied: today(), updated: Date.now() });
    if (!it) { say('Give the job a title.'); return; }
    const had = items.get(key);
    items.set(key, had
      ? clean({ ...had, title: it.title, company: it.company || had.company, updated: Date.now() })
      : it);
    filter = '';
    save();
    e.target.reset();
    say(had ? 'That link was already on your list - updated it.' : `Added <b>${esc(it.title)}</b>.`);
    onEvent('app-add', 'Added application');
  });

  $('app-export').addEventListener('click', exportCsv);
  $('app-import').addEventListener('change', async (e) => {
    const file = e.target.files[0];
    e.target.value = '';                 // so the same file can be picked again
    if (!file) return;
    try { await importCsv(file); } catch { say('Could not read that file.'); }
  });

  // Another tab of this site changed the list.
  addEventListener('storage', (e) => {
    if (e.key !== STORE) return;
    load();
    renderAll();
  });
}

export function initApplied({ track } = {}) {
  if (track) onEvent = track;
  load();
  wire();
  renderAll();
  if (location.hash === '#applications') showTab('applied');
}
