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
 *
 * Each job holds the STAGES it went through - Applied, OA 1, Interview 1,
 * Interview 2, Rejected - each with an optional date and note. Its status,
 * round and scheduled date are read off the last stage, so the table's
 * columns and the timeline in the expanded row can never disagree. Rounds are
 * not stored: "Interview 2" is simply the second interview stage.
 */

const STORE = 'doomers:applications:v1';
const MAX_ITEMS = 5000;
const MAX_STAGES = 30;

export const STATUSES = [
  ['applied', 'Applied'], ['oa', 'OA'], ['interview', 'Interview'],
  ['rejected', 'Rejected'], ['accepted', 'Accepted'],
];
const LABEL = Object.fromEntries(STATUSES);
const BY_LABEL = Object.fromEntries(STATUSES.map(([k, l]) => [l.toLowerCase(), k]));
Object.assign(BY_LABEL, { 'online assessment': 'oa', int: 'interview', offer: 'accepted' });
const ROUNDED = new Set(['oa', 'interview']);   // stages that come in numbered rounds
const TERMINAL = new Set(['rejected', 'accepted']);
const DATE_RE = /^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2})?$/;

const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const today = () => new Date().toLocaleDateString('en-CA');   // YYYY-MM-DD, local
const toStage = (x) => (LABEL[x] ? x : BY_LABEL[String(x ?? '').trim().toLowerCase()]);

let items = new Map();     // key -> item
let storageOk = true;
let filter = '';           // '' = all, else a status
let lastRemoved = null;
const open = new Set();    // keys whose stage timeline is expanded
const seen = new Map();    // key -> listing row, for "Mark applied" on rendered rows
let onEvent = () => {};

/* ── stages ────────────────────────────────────────────────────────────── */
/** 1 for the first OA, 2 for the second, ... */
const ordinal = (stages, i) => stages.slice(0, i + 1).filter((x) => x.s === stages[i].s).length;

function stageName(stages, i, { short = false } = {}) {
  const x = stages[i];
  if (!ROUNDED.has(x.s)) return LABEL[x.s];
  return `${short && x.s === 'interview' ? 'Int' : LABEL[x.s]} ${ordinal(stages, i)}`;
}

const cleanNote = (v) => String(v ?? '').replace(/[<>();\r\n]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 80);

/** Stages from storage (objects), an older save (strings) or a CSV cell
 *  ("Applied > Interview 1 (2026-11-14T15:30; System design)"). */
function parseStages(v) {
  const one = (x) => {
    if (x && typeof x === 'object') return { s: toStage(x.s), date: x.date, note: x.note };
    const m = /^([^(]*?)\s*(?:\((.*)\))?\s*$/.exec(String(x ?? '').trim());
    if (!m) return null;
    const s = toStage(m[1].replace(/\s*\d+$/, ''));
    const [date = '', ...note] = (m[2] ?? '').split(';');
    return DATE_RE.test(date.trim()) ? { s, date: date.trim(), note: note.join(' ') }
      : { s, note: [date, ...note].join(' ') };
  };
  const list = Array.isArray(v) ? v : String(v ?? '').split('>').filter((x) => x.trim());
  return list.map(one).filter((x) => x && x.s)
    .map((x) => ({ s: x.s, date: DATE_RE.test(x.date ?? '') ? x.date : '', note: cleanNote(x.note) }));
}

/** Applied first and only first, an outcome only last. */
function normalize(stages) {
  const body = stages.filter((x, i) => x.s !== 'applied' && (!TERMINAL.has(x.s) || i === stages.length - 1));
  const first = stages[0]?.s === 'applied' ? stages[0] : { s: 'applied', date: '', note: '' };
  return [first, ...body].slice(0, MAX_STAGES);
}

const dropOutcome = (stages) => stages.filter((x) => !TERMINAL.has(x.s));
const blank = (s) => ({ s, date: '', note: '' });

/** The status dropdown. Going back to a stage already on the path cuts the
 *  path there (a correction); a new stage or an outcome is appended. */
function moveTo(stages, s) {
  const p = dropOutcome(stages);
  if (s === 'applied') return p.slice(0, 1);
  if (TERMINAL.has(s)) return [...p, blank(s)];
  const at = p.map((x) => x.s).lastIndexOf(s);
  return at >= 0 ? p.slice(0, at + 1) : [...p, blank(s)];
}

/** The round box: a higher round adds that many stages, a lower one cuts back. */
function setRound(stages, n) {
  const p = dropOutcome(stages);
  const t = p[p.length - 1].s;
  if (!ROUNDED.has(t) || !(n >= 1 && n <= 20)) return stages;
  const at = p.map((x, i) => (x.s === t ? i : -1)).filter((i) => i >= 0);
  return n <= at.length ? p.slice(0, at[n - 1] + 1)
    : [...p, ...Array.from({ length: n - at.length }, () => blank(t))];
}

/* ── storage ───────────────────────────────────────────────────────────── */
function clean(o) {
  if (!o || typeof o !== 'object') return null;
  const s = (v, n = 300) => String(v ?? '').trim().slice(0, n);
  const key = s(o.key, 600);
  const title = s(o.title);
  if (!key || !title) return null;
  const url = s(o.url, 2000);
  const applied = s(o.applied, 10);
  const upd = Number(o.updated);

  let stages = normalize(parseStages(o.stages));
  // Status / Round / Scheduled given alongside (an older save, or a CSV edited
  // in a spreadsheet) win over the stages, the same way the table's own
  // dropdown and round box would apply them.
  const status = toStage(o.status);
  if (status && stages[stages.length - 1].s !== status) stages = moveTo(stages, status);
  const round = Number.parseInt(o.round, 10);
  const last = () => stages[stages.length - 1];
  if (ROUNDED.has(last().s) && round && round !== ordinal(stages, stages.length - 1)) {
    stages = setRound(stages, round);
  }
  const sched = s(o.scheduled, 16);
  if (ROUNDED.has(last().s) && !last().date && DATE_RE.test(sched)) last().date = sched;

  const end = last();
  const rounded = ROUNDED.has(end.s);
  const appliedOn = DATE_RE.test(applied) ? applied.slice(0, 10) : today();
  stages[0].date = appliedOn;
  return {
    key, title,
    company: s(o.company), location: s(o.location), site: s(o.site, 40), country: s(o.country, 40),
    url: /^https?:\/\//i.test(url) ? url : '',          // never a javascript: link from an import
    stages,
    status: end.s,
    round: rounded ? ordinal(stages, stages.length - 1) : null,
    scheduled: rounded ? end.date : '',
    applied: appliedOn,
    updated: Number.isFinite(upd) && upd > 0 ? upd : Date.parse(o.updated) || 0,
  };
}

/** Rebuild an item from new stages; the derived fields are recomputed. */
function withStages(it, stages, extra = {}) {
  const { status, round, scheduled, ...rest } = it;
  return clean({ ...rest, ...extra, stages, updated: Date.now() });
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

let renderTimer = null;
/** Persist, then redraw. A field's change event fires as focus is LEAVING it
 *  (Tab, or a click on the next field), before focus lands on the next one;
 *  redrawing right then would destroy that target and drop focus to <body>.
 *  `defer` waits a tick so focus has landed and renderTable can restore it. */
function save({ defer = false } = {}) {
  try {
    localStorage.setItem(STORE, JSON.stringify({ v: 1, items: [...items.values()] }));
    storageOk = true;
  } catch {
    storageOk = false;
  }
  clearTimeout(renderTimer);
  if (defer) renderTimer = setTimeout(renderAll, 0);
  else renderAll();
}

/* ── listing rows: the "Mark applied" button ───────────────────────────── */
const listingKey = (r, country) => `j:${country}:${r.id}`;

function markLabel(it) {
  return it ? `✓ ${stageName(it.stages, it.stages.length - 1)}` : 'Mark applied';
}

/** HTML for the button on a listing row. Remembers the row so a click can save it. */
export function markButton(r, country) {
  if (!r?.id || !r.title) return '';
  const key = listingKey(r, country);
  seen.set(key, { ...r, country });
  const it = items.get(key);
  return `<button type="button" class="mark${it ? ' on' : ''}" data-mark="${esc(key)}"
    aria-label="${it ? `Tracked as ${LABEL[it.status]} - show in My applications` : `Mark ${esc(r.title)} as applied`}">${esc(markLabel(it))}</button>`;
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
    country: r.country, url: r.job_url, applied: today(), updated: Date.now(),
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
  if (name === 'applied') renderFlow();
  const u = new URL(location);
  u.hash = name === 'applied' ? 'applications' : '';
  history.replaceState(null, '', u);
}

/** For the jump links at the top of the page. */
export function goToTab(name) {
  showTab(name);
  $('panel-listings').scrollIntoView({ behavior: 'smooth', block: 'start' });
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

const asLocal = (d) => (d && d.length === 10 ? `${d}T09:00` : d || '');

function rowHtml(it, n) {
  const name = esc(it.title);
  const rounded = ROUNDED.has(it.status);
  const isOpen = open.has(it.key);
  const opts = STATUSES.map(([k, l]) =>
    `<option value="${k}"${k === it.status ? ' selected' : ''}>${l}</option>`).join('');
  const main = `<tr class="main${isOpen ? ' open' : ''}" data-key="${esc(it.key)}">
    <td class="title" data-l="Role"><button type="button" class="exp" data-act="toggle"
      aria-expanded="${isOpen}" aria-controls="app-det-${n}"><span class="chev" aria-hidden="true"></span>${name}</button>
      <span class="path">${esc(it.stages.map((_, i) => stageName(it.stages, i)).join(' → '))}</span></td>
    <td class="co" data-l="Company">${esc(it.company) || '—'}</td>
    <td data-l="Status"><select class="st st-${it.status}" data-f="status" aria-label="Status of ${name}">${opts}</select></td>
    <td data-l="Round"${rounded ? '' : ' class="na"'}>${rounded
      ? `<input type="number" data-f="round" min="1" max="20" inputmode="numeric" value="${it.round ?? ''}" aria-label="Round for ${name}">`
      : '—'}</td>
    <td data-l="Scheduled"${rounded ? '' : ' class="na"'}>${rounded
      ? `<input type="datetime-local" data-f="scheduled" value="${esc(asLocal(it.scheduled))}" aria-label="${LABEL[it.status]} date for ${name}">`
      : '—'}</td>
    <td data-l="Applied"><input type="date" data-f="applied" value="${esc(it.applied)}" aria-label="Date applied to ${name}"></td>
    <td class="actions">${it.url ? `<a href="${esc(it.url)}" target="_blank" rel="noopener noreferrer nofollow">Open&nbsp;↗</a>` : ''}
      <button type="button" class="linkish" data-act="remove" aria-label="Remove ${name}">Remove</button></td>
  </tr>`;

  const st = it.stages;
  const steps = st.map((x, i) => {
    const sn = esc(stageName(st, i));
    const date = i === 0
      ? `<input type="date" data-f="applied" value="${esc(it.applied)}" aria-label="Date applied">`
      : TERMINAL.has(x.s)
        ? `<input type="date" data-sf="date" data-i="${i}" value="${esc(x.date.slice(0, 10))}" aria-label="${sn} date">`
        : `<input type="datetime-local" data-sf="date" data-i="${i}" value="${esc(asLocal(x.date))}" aria-label="${sn} date">`;
    const hint = i === 0 ? 'e.g. via referral' : x.s === 'oa' ? 'e.g. HackerRank, 90 min'
      : x.s === 'interview' ? 'e.g. system design' : 'note';
    return `<li class="st-${x.s}"><span class="stage-dot" aria-hidden="true"></span>
      <span class="stage-name">${sn}</span>${date}
      <input type="text" data-sf="note" data-i="${i}" maxlength="80" value="${esc(x.note)}" placeholder="${hint}" aria-label="${sn} note">
      ${i > 0 ? `<button type="button" class="linkish" data-act="del-stage" data-i="${i}" aria-label="Remove stage ${sn}">Remove</button>` : ''}</li>`;
  }).join('');
  const next = (s) => {
    if (!ROUNDED.has(s)) return LABEL[s];
    return `${LABEL[s]} ${st.filter((x) => x.s === s).length + 1}`;
  };
  const adds = ['oa', 'interview', 'rejected', 'accepted'].map((s) =>
    `<button type="button" class="tool st-${s}" data-add="${s}">+ ${next(s)}</button>`).join('');
  const detail = `<tr class="detail" id="app-det-${n}" data-key="${esc(it.key)}"${isOpen ? '' : ' hidden'}>
    <td colspan="7"><ol class="stages" aria-label="Stages of ${name}">${steps}</ol>
      <div class="stage-add"><span>Add a stage</span>${adds}</div></td></tr>`;
  return main + detail;
}

function renderTable() {
  const tb = $('apps').querySelector('tbody');
  // Re-rendering replaces every input; put focus back on the same control so
  // tabbing through a row's fields is not interrupted by each save.
  const ctrls = () => [...tb.querySelectorAll('input, select, button')];
  const a = document.activeElement;
  const was = tb.contains(a) ? { i: ctrls().indexOf(a), key: a.closest('tr')?.dataset.key } : null;

  const rows = sorted().filter((it) => !filter || it.status === filter);
  tb.innerHTML = rows.length
    ? rows.map(rowHtml).join('')
    : `<tr><td colspan="7" class="empty">${items.size
      ? 'No jobs with this status.'
      : 'Nothing tracked yet. Press <b>Mark applied</b> on any listing above, or add a job yourself.'}</td></tr>`;

  if (was) {
    const el = ctrls()[was.i];
    if (el && el.closest('tr')?.dataset.key === was.key) el.focus({ preventScroll: true });
  }
}

function renderAll() {
  $('app-count').textContent = items.size ? String(items.size) : '';
  $('app-export').disabled = items.size === 0;
  $('app-storage').hidden = storageOk;
  renderFilter();
  renderTable();
  renderFlow();
  syncMarks();
}

/* ── Sankey: where the applications went ───────────────────────────────── */
// Stage nodes are "applied", "oa1", "oa2", "interview1", ...; each path ends
// in an outcome, or in "still waiting" at the stage it reached.
const OUTCOMES = [
  ['accepted', '✓ Accepted'], ['rejected', '✕ Rejected'],
  ['progress', 'In progress'], ['waiting', 'No reply yet'],
];
const FLOW_COLOR = {
  applied: '--ink-soft', oa: '--st-oa', interview: '--accent', accepted: '--good',
  rejected: '--st-rejected', progress: '--ink-mute', waiting: '--border-strong',
};
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const nodeId = (st, i) => (ROUNDED.has(st[i].s) ? `${st[i].s}${ordinal(st, i)}` : st[i].s);
const nodeType = (id) => id.replace(/\d+$/, '');
const nodeRound = (id) => Number(id.match(/\d+$/)?.[0] || 0);

function flowData() {
  const links = new Map();   // "a>b" -> count
  const add = (a, b) => links.set(`${a}>${b}`, (links.get(`${a}>${b}`) || 0) + 1);
  for (const it of items.values()) {
    const ids = it.stages.map((_, i) => nodeId(it.stages, i));
    for (let i = 1; i < ids.length; i++) add(ids[i - 1], ids[i]);
    const last = ids[ids.length - 1];
    if (!TERMINAL.has(last)) add(last, last === 'applied' ? 'waiting' : 'progress');
  }
  return [...links].map(([k, n]) => { const [a, b] = k.split('>'); return { a, b, n }; });
}

function renderFlow() {
  const box = $('app-flow');
  const host = $('app-sankey');
  const total = items.size;
  box.hidden = total === 0;
  if (!total || !host.clientWidth) return;     // hidden tab: drawn when shown

  const links = flowData();
  const outcome = new Set(OUTCOMES.map(([k]) => k));
  const stagesUsed = [...new Set(['applied', ...links.flatMap((l) => [l.a, l.b])])]
    .filter((id) => !outcome.has(id));

  // Column = the longest run of stages that leads to the node, so OA 1 ->
  // Interview 1 -> Interview 2 reads left to right; every outcome shares the
  // last column. Capped passes keep an odd cycle (one job doing OA after an
  // interview, another before) from looping.
  const col = { applied: 0 };
  for (let pass = 0; pass < stagesUsed.length; pass++) {
    let moved = false;
    for (const l of links) {
      if (outcome.has(l.b) || col[l.a] == null) continue;
      if (col[l.b] == null || col[l.b] < col[l.a] + 1) { col[l.b] = col[l.a] + 1; moved = true; }
    }
    if (!moved) break;
  }
  const lastCol = Math.max(...stagesUsed.map((id) => col[id] ?? 0)) + 1;
  for (const [k] of OUTCOMES) col[k] = lastCol;
  const ncols = lastCol + 1;

  // Order within a column: OA rounds, then interview rounds, then outcomes.
  const typeRank = { applied: 0, oa: 1, interview: 2 };
  const ids = [
    ...stagesUsed.sort((p, q) => col[p] - col[q] || typeRank[nodeType(p)] - typeRank[nodeType(q)]
      || nodeRound(p) - nodeRound(q)),
    ...OUTCOMES.map(([k]) => k),
  ];
  const val = {};
  for (const id of ids) {
    const inn = links.filter((l) => l.b === id).reduce((s, l) => s + l.n, 0);
    const out = links.filter((l) => l.a === id).reduce((s, l) => s + l.n, 0);
    val[id] = id === 'applied' ? total : Math.max(inn, out);
  }
  const nodes = ids.filter((id) => val[id] > 0);

  const W = host.clientWidth - 24;      // inside the 12px padding
  const phone = W < 560;
  const spacing = (W - (phone ? 96 : 130)) / Math.max(ncols - 1, 1);
  const short = spacing < 70;
  // "Interview" alone when nobody had a second one; numbered once anyone did.
  const maxRound = {};
  for (const id of nodes) maxRound[nodeType(id)] = Math.max(maxRound[nodeType(id)] || 0, nodeRound(id));
  const name = (id) => {
    const o = OUTCOMES.find(([k]) => k === id);
    if (o) return o[1];
    const t = nodeType(id);
    if (!ROUNDED.has(t)) return LABEL[t];
    const base = short && t === 'interview' ? 'Int' : LABEL[t];
    return maxRound[t] > 1 ? `${base} ${nodeRound(id)}` : base;
  };
  const plain = (id) => name(id).replace(/^[✓✕] /, '').replace(/^Int\b/, 'Interview');

  const H = phone ? 240 : 280;
  const NW = 10;                       // node width
  const GAP = 16;                      // between nodes in a column
  const labelRoom = phone ? 96 : 130;  // right of the last column
  // Room for the labels above the stage columns; when columns are close the
  // count goes on a second line there, or "Applied 20" runs into "OA 5".
  const stacked = phone || short;
  const top = stacked ? 36 : 22;
  // The first gap is kept wide enough for "Applied", the longest stage label;
  // later columns ("OA", "Int 2") are narrower and share what is left.
  const avail = W - labelRoom - NW;
  const first = ncols > 2 ? Math.min(Math.max(avail / (ncols - 1), 62), avail / 2) : avail;
  const colX = (i) => (i === 0 ? 0 : first + (i - 1) * (ncols > 2 ? (avail - first) / (ncols - 2) : 0));
  const maxInCol = Math.max(...[...Array(ncols)].map((_, i) => nodes.filter((id) => col[id] === i).length));
  const k = (H - top - 6 - GAP * (maxInCol - 1)) / total;

  const pos = {};
  for (let i = 0; i < ncols; i++) {
    // Top-aligned: with the stages and the outcomes all stacked from the top,
    // the ribbons out of Applied leave in the same order they arrive.
    let y = top;
    for (const id of nodes.filter((n) => col[n] === i)) {
      pos[id] = { x: colX(i), y, h: val[id] * k, out: 0, in: 0 };
      y += val[id] * k + GAP;
    }
  }

  // Ribbons stack in target order out of a node and source order into one,
  // so they do not cross more than they have to.
  const outs = [...links].sort((p, q) => pos[p.b].y - pos[q.b].y || pos[p.b].x - pos[q.b].x);
  for (const l of outs) { l.sy = pos[l.a].y + pos[l.a].out; pos[l.a].out += l.n * k; }
  const ins = [...links].sort((p, q) => pos[p.a].x - pos[q.a].x || pos[p.a].y - pos[q.a].y);
  for (const l of ins) { l.ty = pos[l.b].y + pos[l.b].in; pos[l.b].in += l.n * k; }

  const pct = (n) => `${Math.round((n / total) * 100)}%`;
  const ribbons = links.map((l) => {
    const x0 = pos[l.a].x + NW; const x1 = pos[l.b].x; const h = l.n * k;
    const m = (x0 + x1) / 2;
    const d = `M${x0},${l.sy}C${m},${l.sy} ${m},${l.ty} ${x1},${l.ty}`
      + `L${x1},${l.ty + h}C${m},${l.ty + h} ${m},${l.sy + h} ${x0},${l.sy + h}Z`;
    const tip = `${plain(l.a)} → ${plain(l.b)}: ${l.n} (${pct(l.n)} of applications)`;
    return `<path class="flow-link" d="${d}" fill="${css(FLOW_COLOR[nodeType(l.b)])}" data-tip="${esc(tip)}"><title>${esc(tip)}</title></path>`;
  }).join('');
  const bars = nodes.map((id) => {
    const p = pos[id];
    const last = col[id] === ncols - 1;
    const tip = `${plain(id)}: ${val[id]} (${pct(val[id])})`;
    // Outcomes are labelled to their right; the stages above their node, where
    // no ribbon runs. Text wears ink colours - the bar carries the hue.
    const two = stacked && !last;
    const at = last ? `x="${p.x + NW + 6}" y="${p.y + p.h / 2}" dy="0.35em"`
      : `x="${p.x}" y="${p.y - (two ? 21 : 7)}"`;
    const n = two ? `<tspan class="fl-n" x="${p.x}" dy="1.2em">${val[id]}</tspan>`
      : `<tspan class="fl-n"> · ${val[id]}</tspan>`;
    return `<g data-tip="${esc(tip)}"><rect x="${p.x}" y="${p.y}" width="${NW}" height="${Math.max(p.h, 2)}" rx="2"
        fill="${css(FLOW_COLOR[nodeType(id)])}"><title>${esc(tip)}</title></rect>
      <text ${at}><tspan class="fl-name">${esc(name(id))}</tspan>${n}</text></g>`;
  }).join('');
  const summary = nodes.map((id) => `${plain(id)}: ${val[id]}`).join(', ');
  host.innerHTML = `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img"
    aria-label="Where your ${total} applications went: ${esc(summary)}">${ribbons}${bars}</svg>
    <div class="tip flow-tip" aria-hidden="true"></div>`;
  $('app-flow-note').textContent = `${summary}.`;
}

function say(html) {
  $('app-status').innerHTML = html;
}

/* ── CSV ───────────────────────────────────────────────────────────────── */
const COLS = [
  ['title', 'Title'], ['company', 'Company'], ['status', 'Status'], ['round', 'Round'],
  ['scheduled', 'Scheduled'], ['applied', 'Applied on'], ['location', 'Location'],
  ['site', 'Source'], ['url', 'URL'], ['country', 'Country'], ['stages', 'Stages'],
  ['updated', 'Updated'], ['key', 'Key'],
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

/** "Applied > OA 1 (2026-11-10) > Interview 1 (2026-11-14T15:30; system design)" */
function stagesCell(it) {
  return it.stages.map((x, i) => {
    const date = i === 0 ? '' : x.date;       // the applied date has its own column
    const inner = x.note ? `${date}; ${x.note}` : date;
    return stageName(it.stages, i) + (inner ? ` (${inner})` : '');
  }).join(' > ');
}

function toCsv() {
  const lines = [COLS.map(([, h]) => h).join(',')];
  for (const it of sorted()) {
    lines.push(COLS.map(([f]) => cell(
      f === 'status' ? LABEL[it.status]
        : f === 'stages' ? stagesCell(it)
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
/** A field in the job's main row. */
function edit(key, field, value) {
  const it = items.get(key);
  if (!it) return;
  const st = it.stages.map((x) => ({ ...x }));
  if (field === 'status') items.set(key, withStages(it, moveTo(st, value)));
  else if (field === 'round') items.set(key, withStages(it, setRound(st, Number.parseInt(value, 10))));
  else if (field === 'scheduled') {
    st[st.length - 1].date = DATE_RE.test(value) ? value : '';
    items.set(key, withStages(it, st));
  } else if (field === 'applied') items.set(key, withStages(it, st, { applied: value }));
  // A status change reshapes the row and is followed by an explicit refocus.
  save({ defer: field !== 'status' });
}

/** A field of one stage in the expanded timeline. */
function editStage(key, i, field, value) {
  const it = items.get(key);
  if (!it || !it.stages[i]) return;
  const st = it.stages.map((x) => ({ ...x }));
  st[i][field] = field === 'note' ? cleanNote(value) : DATE_RE.test(value) ? value : '';
  items.set(key, withStages(it, st));
  save({ defer: true });
}

function addStage(key, s) {
  const it = items.get(key);
  if (!it) return;
  // A new round is always appended - that is how Interview 2 follows Interview 1.
  // Adding anything after an outcome replaces the outcome.
  items.set(key, withStages(it, [...dropOutcome(it.stages.map((x) => ({ ...x }))), blank(s)]));
  save();
  onEvent(`app-stage-${s}`, 'Added stage');
}

function removeStage(key, i) {
  const it = items.get(key);
  if (!it || i < 1) return;
  items.set(key, withStages(it, it.stages.filter((_, j) => j !== i)));
  save();
}

function focusRow(key, sel) {
  const tb = $('apps').querySelector('tbody');
  tb.querySelector(`tr[data-key="${CSS.escape(key)}"] ${sel}`)?.focus({ preventScroll: true });
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
    focusRow(key, 'select');
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
    const t = e.target;
    const key = t.closest('tr')?.dataset.key;
    if (!key) return;
    if (t.dataset.sf) { editStage(key, Number(t.dataset.i), t.dataset.sf, t.value); return; }
    if (!t.dataset.f) return;
    edit(key, t.dataset.f, t.value);
    if (t.dataset.f === 'status') {
      onEvent(`app-status-${t.value}`, 'Application status');
      if (!tb.querySelector(`tr[data-key="${CSS.escape(key)}"]`)) say(`Moved to <b>${LABEL[t.value]}</b>.`);   // filtered out
      else focusRow(key, 'select');
    }
  });
  tb.addEventListener('click', (e) => {
    const b = e.target.closest('[data-act], [data-add]');
    if (!b) return;
    const key = b.closest('tr').dataset.key;
    if (b.dataset.add) {
      addStage(key, b.dataset.add);
      focusRow(key, `[data-add="${b.dataset.add}"]`);
    } else if (b.dataset.act === 'toggle') {
      if (open.has(key)) open.delete(key); else open.add(key);
      renderTable();
      focusRow(key, '.exp');
    } else if (b.dataset.act === 'del-stage') {
      removeStage(key, Number(b.dataset.i));
      focusRow(key, '[data-add="interview"]');
    } else if (b.dataset.act === 'remove') {
      lastRemoved = items.get(key);
      items.delete(key);
      open.delete(key);
      save();
      say(`Removed <b>${esc(lastRemoved.title)}</b>. <button type="button" class="linkish" id="app-undo">Undo</button>`);
    }
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
      applied: today(), updated: Date.now() });
    if (!it) { say('Give the job a title.'); return; }
    const had = items.get(key);
    items.set(key, had
      ? withStages(had, had.stages, { title: it.title, company: it.company || had.company })
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

  // Hover tooltip on the Sankey's ribbons and nodes.
  const sk = $('app-sankey');
  sk.addEventListener('pointermove', (e) => {
    const t = e.target.closest('[data-tip]');
    const tip = sk.querySelector('.flow-tip');
    if (!tip) return;
    if (!t) { tip.style.opacity = 0; return; }
    tip.textContent = t.dataset.tip;
    const r = sk.getBoundingClientRect();
    const x = Math.min(e.clientX - r.left + 12, r.width - tip.offsetWidth - 4);
    tip.style.transform = `translate(${Math.max(0, x)}px, ${e.clientY - r.top + 14}px)`;
    tip.style.opacity = 1;
  });
  sk.addEventListener('pointerleave', () => { const tip = sk.querySelector('.flow-tip'); if (tip) tip.style.opacity = 0; });
  let rt = null;
  addEventListener('resize', () => { clearTimeout(rt); rt = setTimeout(renderFlow, 150); });

  // Jump links at the top of the page that land on a tab.
  document.addEventListener('click', (e) => {
    const a = e.target.closest('a[data-tab]');
    if (!a) return;
    e.preventDefault();
    goToTab(a.dataset.tab);
  });
  addEventListener('hashchange', () => {
    if (location.hash === '#applications') goToTab('applied');
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
