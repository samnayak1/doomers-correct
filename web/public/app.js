import { drawChart, highlightRole, pinRole, prettyRole } from '/chart.js';
import { initApplied, markButton } from '/applied.js';

const $ = (id) => document.getElementById(id);
const api = (path, params) => {
  const u = new URL(path, location.origin);
  for (const [k, v] of Object.entries(params || {})) if (v !== '' && v != null) u.searchParams.set(k, v);
  return fetch(u).then((r) => {
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
  });
};

const state = {
  country: new URLSearchParams(location.search).get('country') || 'india',
  q: '', site: '', remote: '', role: '', sort: 'date_posted',
  offset: 0, limit: 25, total: 0, currency: 'INR',
};
let lastSeries = null;
let jobsTimer = null;

const nf = new Intl.NumberFormat('en-US');
const plural = (n, one, many = `${one}s`) => `${nf.format(n)} ${n === 1 ? one : many}`;
const fmtDate = (iso) => (iso ? new Date(iso + 'T00:00:00Z')
  .toLocaleDateString('en-US', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' }) : '—');

function fmtSalary(row) {
  const { min_amount: lo, max_amount: hi, currency } = row;
  if (!lo && !hi) return '—';
  const sym = { INR: '₹', USD: '$' }[currency] || '';
  const short = (n) => {
    if (n == null) return '';
    if (currency === 'INR' && n >= 100000) return `${(n / 100000).toFixed(n >= 1000000 ? 0 : 1)}L`;
    if (n >= 1000) return `${Math.round(n / 1000)}k`;
    return String(Math.round(n));
  };
  const body = lo && hi && Math.round(lo) !== Math.round(hi) ? `${short(lo)}–${short(hi)}` : short(hi || lo);
  return `${sym}${body}`;
}

/* ── headline ─────────────────────────────────────────────────────────────
   Oversized numbers set inline in a sentence rather than a row of cards. */
function renderHeadline(d) {
  const s = d.summary || {};
  const fc = d.forecast;
  const end = fc && fc.points.length ? fc.points[fc.points.length - 1] : null;
  const delta = s.delta_pct;
  const parts = [
    `<b>${nf.format(s.tech_active || 0)}</b> active tech listings`,
    `<b>${nf.format(s.new_jobs || 0)}</b> new in the last run`,
  ];
  if (delta != null) {
    parts.push(`<b class="${delta >= 0 ? 'up' : 'down'}">${delta > 0 ? '+' : ''}`
      + `${delta.toFixed(1)}%</b> vs the run before`);
  }
  if (s.tech_active) {
    parts.push(`<b>${Math.round((s.remote_active / s.tech_active) * 100)}%</b> remote`);
  }
  if (end) parts.push(`<b>${nf.format(Math.round(end.yhat))}</b> projected for Dec&nbsp;2027`);
  $('headline').innerHTML = parts.join('<span class="sep">&middot;</span>');
}

/* ── chart ─────────────────────────────────────────────────────────────── */
function renderChart(d) {
  const s = d.series || { dates: [], values: [] };
  const history = s.dates.map((date, i) => ({ date, value: s.values[i] }));
  const drawn = drawChart($('chart'), {
    history,
    forecast: d.forecast ? d.forecast.points : [],
    roles: d.roles || [],
  }) || {};
  renderRoleLegend(drawn.roles || []);
  $('lg-band').lastChild.textContent = drawn.bandClipped
    ? '95% interval (runs past the top — hover for the range)' : '95% interval';
  // Legend entries only for marks that are actually on the chart.
  $('lg-forecast').hidden = !d.forecast;
  $('lg-band').hidden = !d.forecast;

  $('chart-sub').textContent = history.length
    ? `${fmtDate(history[0].date)} – ${fmtDate(history[history.length - 1].date)} · `
      + plural(s.observed_days || 0, 'scrape')
    : '';

  const fc = d.forecast;
  $('model-note').textContent = fc
    ? `Projection: ${fc.model}, fitted on ${plural(fc.fitted_on.points, 'observed day')} `
      + `(${fmtDate(fc.fitted_on.start)} – ${fmtDate(fc.fitted_on.end)}), extended ${plural(fc.horizon_days, 'day')} `
      + `to ${d.config.forecast_until}. Trend damping ${fc.params.damping ?? 1}. `
      + `This is an extrapolation of the current trend, not a prediction checkout the method note below.`
    : `No forecast yet: the model needs at least ${d.config.forecast_min_points} days of collected `
      + `data (linear regression), and ${d.config.arima_min_points} days before it switches to ARIMA. `
      + `The chart begins at the first completed scrape.`;
}

/* ── role legend ───────────────────────────────────────────────────────── */
// Buttons, so the highlight works by keyboard and by tap as well as by hover.
// A tap toggles, since a touch screen has no hover to leave.
function renderRoleLegend(roles) {
  const box = $('lg-roles');
  box.hidden = roles.length === 0;
  if (!roles.length) { box.replaceChildren(); return; }
  const title = document.createElement('span');
  title.className = 'lr-title';
  title.textContent = 'By role (since role tracking began) - hover or tap:';
  const buttons = roles.map(({ role, color }) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.dataset.role = role;
    const key = document.createElement('i');
    key.style.borderTopColor = color;
    b.append(key, document.createTextNode(prettyRole(role)));
    b.addEventListener('mouseenter', () => highlightRole($('chart'), role));
    b.addEventListener('mouseleave', () => highlightRole($('chart'), null));
    // Keyboard focus only: a tap focuses the button too, and highlighting on
    // that focus made the click that follows toggle the line straight back off.
    b.addEventListener('focus', () => { if (b.matches(':focus-visible')) highlightRole($('chart'), role); });
    b.addEventListener('blur', () => highlightRole($('chart'), null));
    // Click pins the line so it stays bold without hover (a tap is hover and
    // click at once); clicking the pinned role again unpins it.
    b.addEventListener('click', () => {
      const pinned = $('chart')._chart?.$pinned;
      pinRole($('chart'), pinned === role ? null : role);
    });
    return b;
  });
  box.replaceChildren(title, ...buttons);
  // Mirror the canvas highlight back onto the legend.
  const chart = $('chart')._chart;
  if (chart) {
    chart.$onHighlight = (active) => buttons.forEach((b) =>
      b.classList.toggle('on', b.dataset.role === active));
  }
}

/* ── jobs table ────────────────────────────────────────────────────────── */
// Shared by the listings table and the resume-match results.
const rowLead = (r) => `
        <td class="title" data-l="Role">${esc(r.title)}</td>
        <td class="co" data-l="Company">${esc(r.company)}</td>
        <td class="co" data-l="Location">${esc(r.location)}</td>
        <td data-l="Source"><span class="pill">${esc(r.site)}</span></td>
        <td class="num" data-l="Posted">${fmtDate(r.date_posted)}</td>`;
const rowActions = (r) => `<td class="actions">${r.is_remote ? '<span class="pill remote">Remote</span>' : ''}
            ${r.job_url ? `<a href="${esc(r.job_url)}" target="_blank" rel="noopener noreferrer nofollow">Open&nbsp;↗</a>` : ''}
            ${markButton(r, state.country)}</td>`;

function renderJobs(d) {
  state.total = d.total;
  state.currency = d.currency;
  const tbody = $('jobs').querySelector('tbody');
  if (!d.rows.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty">No listings match these filters.</td></tr>`;
  } else {
    tbody.innerHTML = d.rows.map((r) => {
      const row = { ...r, currency: d.currency };
      // data-l drives the stacked card view under 640px, where the header row
      // is hidden and each cell has to say what it is.
      return `<tr>${rowLead(r)}
        <td class="num" data-l="Salary">${fmtSalary(row)}</td>
        ${rowActions(r)}</tr>`;
    }).join('');
  }
  const from = d.total ? state.offset + 1 : 0;
  const to = Math.min(state.offset + state.limit, d.total);
  $('count').textContent = `${nf.format(from)}–${nf.format(to)} of ${nf.format(d.total)}`;
  $('prev').disabled = state.offset === 0;
  $('next').disabled = state.offset + state.limit >= d.total;
}

const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* ── loaders ───────────────────────────────────────────────────────────── */
async function loadSeries() {
  $('chart').innerHTML = '<div class="empty">Loading…</div>';
  try {
    const d = await api('/api/series', { country: state.country });
    lastSeries = d;
    $('freshness').textContent = d.summary.latest_scrape
      ? `Last scrape ${fmtDate(d.summary.latest_scrape)} · ${plural(d.summary.tracked_total, 'listing')} tracked`
      : d.summary.tracked_total
        ? `First scrape in progress · ${plural(d.summary.tracked_total, 'listing')} so far`
        : 'Awaiting first scrape';
    renderHeadline(d);
    renderChart(d);
    $('site').innerHTML = '<option value="">All sources</option>'
      + (d.summary.by_site || []).map((s) => `<option value="${esc(s.site)}">${esc(s.site)} (${nf.format(s.n)})</option>`).join('');
    $('site').value = state.site || '';

    // Roles come from what has actually been labelled, so the dropdown never
    // offers a category with nothing behind it.
    const roles = d.summary.by_role || [];
    $('role').innerHTML = '<option value="">All roles</option>'
      + roles.map((r) => `<option value="${esc(r.role)}">${esc(prettyRole(r.role))} (${nf.format(r.n)})</option>`).join('');
    $('role').value = state.role || '';
    $('role').hidden = roles.length === 0;
  } catch (err) {
    $('chart').innerHTML = `<div class="empty">Could not load the series — ${esc(err.message)}</div>`;
    $('headline').textContent = '';
  }
}

async function loadJobs() {
  try {
    renderJobs(await api('/api/jobs', {
      country: state.country, q: state.q, site: state.site,
      remote: state.remote, role: state.role, sort: state.sort,
      limit: state.limit, offset: state.offset,
    }));
  } catch (err) {
    $('jobs').querySelector('tbody').innerHTML =
      `<tr><td colspan="7" class="empty">Could not load listings — ${esc(err.message)}</td></tr>`;
  }
}

/* ── wiring ────────────────────────────────────────────────────────────── */
async function loadRuns() {
  const tb = $('runs').querySelector('tbody');
  try {
    const { runs } = await api('/api/history', { country: state.country, limit: 10 });
    if (!runs.length) {
      tb.innerHTML = '<tr><td colspan="7" class="empty">No scrapes recorded yet.</td></tr>';
      return;
    }
    const dur = (s) => (s == null ? '—' : s < 90 ? `${Math.round(s)}s` : `${Math.round(s / 60)}m`);
    // active_total stopped being written when the all-roles view was removed, so
    // older rows carry it and newer ones do not. It is deliberately not shown.
    tb.innerHTML = runs.map((r) => `<tr>
      <td class="num">${esc(fmtDate(r.scrape_date))}</td>
      <td class="num">${r.rows_seen == null ? '—' : nf.format(r.rows_seen)}</td>
      <td class="num">${r.new_jobs == null ? '—' : nf.format(r.new_jobs)}</td>
      <td class="num">${r.tech_active == null ? '—' : nf.format(r.tech_active)}</td>
      <td class="num">${r.remote_active == null ? '—' : nf.format(r.remote_active)}</td>
      <td class="num">${esc(dur(r.duration_sec))}</td>
      <td><span class="pill ${r.ok ? 'ok' : 'bad'}">${r.ok ? 'OK' : 'Failed'}</span></td>
    </tr>`).join('');
  } catch (err) {
    tb.innerHTML = `<tr><td colspan="7" class="empty">Could not load run history — ${esc(err.message)}</td></tr>`;
  }
}

function reloadAll() {
  state.offset = 0;
  const u = new URL(location);
  u.searchParams.set('country', state.country);
  history.replaceState(null, '', u);
  loadSeries(); loadJobs(); loadRuns();
}

/* ── analytics ─────────────────────────────────────────────────────────── */
// Events only record WHICH control was used and the option picked - never the
// search box or anything typed into the contact form. A no-op until the
// script has loaded, and permanently if it is blocked or not configured.
const track = (path, title) => {
  try { window.goatcounter?.count?.({ path, title, event: true }); } catch { /* never break the page */ }
};

$('country').addEventListener('change', (e) => { state.country = e.target.value; state.site = ''; state.role = ''; reloadAll(); track(`country-${state.country}`, 'Country'); });
$('site').addEventListener('change', (e) => { state.site = e.target.value; state.offset = 0; loadJobs(); track(`source-${state.site || 'all'}`, 'Source filter'); });
$('role').addEventListener('change', (e) => { state.role = e.target.value; state.offset = 0; loadJobs(); track(`role-${state.role || 'all'}`, 'Role filter'); });
$('remote').addEventListener('change', (e) => { state.remote = e.target.value; state.offset = 0; loadJobs(); });
$('sort').addEventListener('change', (e) => { state.sort = e.target.value; state.offset = 0; loadJobs(); });
$('q').addEventListener('input', (e) => {
  state.q = e.target.value.trim();
  clearTimeout(jobsTimer);
  jobsTimer = setTimeout(() => { state.offset = 0; loadJobs(); }, 250);
});
$('prev').addEventListener('click', () => { state.offset = Math.max(0, state.offset - state.limit); loadJobs(); });
$('next').addEventListener('click', () => { state.offset += state.limit; loadJobs(); });

let resizeTimer = null;
addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { if (lastSeries) renderChart(lastSeries); }, 150);
});

/* ── captcha (shared by contact and resume match) ──────────────────────── */
// Nothing here runs until someone opens a form: readers of the chart never
// download the widget or start a proof-of-work they did not ask for.
let capEndpoint = null;
let capScript = null;
const caps = {};   // slot id -> { token }

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const el = document.createElement('script');
    el.src = src;
    el.onload = resolve;
    el.onerror = () => reject(new Error(`could not load ${src}`));
    document.head.appendChild(el);
  });
}

async function loadCap(endpoint) {
  capEndpoint = endpoint;
  capScript ||= loadScript('/vendor/cap.min.js');
  await capScript;
}

// Tokens are single-use, so every attempt - sent or refused - gets a fresh widget.
function mountCap(slot) {
  const c = caps[slot] = { token: '' };
  const w = document.createElement('cap-widget');
  w.setAttribute('data-cap-api-endpoint', capEndpoint);
  w.addEventListener('solve', (e) => { c.token = e.detail.token; });
  w.addEventListener('reset', () => { c.token = ''; });
  w.addEventListener('error', () => { c.token = ''; });
  $(slot).replaceChildren(w);
}

// Solve on submit rather than making the visitor click the widget first.
async function capToken(slot, status) {
  const c = caps[slot];
  if (c && !c.token) {
    status.textContent = 'Verifying…';
    try { await $(slot).querySelector('cap-widget').solve(); } catch { /* reported below */ }
  }
  if (!c || !c.token) throw new Error('Verification did not complete. Please try again.');
  return c.token;
}

/* ── contact ───────────────────────────────────────────────────────────── */
let contactReady = null;

function prepareContact() {
  contactReady ||= (async () => {
    try {
      const cfg = await api('/api/contact');
      if (!cfg.enabled) throw new Error('not configured');
      await loadCap(cfg.endpoint);
      mountCap('cap-slot');
      return true;
    } catch {
      $('contact-send').disabled = true;
      $('contact-status').textContent = 'The contact form is unavailable right now.';
      return false;
    }
  })();
  return contactReady;
}

$('contact').addEventListener('toggle', (e) => { if (e.currentTarget.open) { prepareContact(); track('contact-open', 'Contact form opened'); } });
$('contact-link').addEventListener('click', (e) => {
  e.preventDefault();
  $('contact').open = true;
  $('contact').scrollIntoView({ behavior: 'smooth', block: 'start' });
});
if (location.hash === '#contact') $('contact').open = true;

$('contact-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.currentTarget;
  const status = $('contact-status');
  const btn = $('contact-send');
  // form.name is the form's own name attribute, so fields go through elements.
  const val = (n) => f.elements.namedItem(n).value;
  if (!f.reportValidity()) return;
  if (!(await prepareContact())) return;

  btn.disabled = true;
  try {
    const token = await capToken('cap-slot', status);
    status.textContent = 'Sending…';
    const r = await fetch('/api/contact', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: val('name'), email: val('email'), message: val('message'), token }),
    });
    const d = await r.json().catch(() => ({}));
    // FastAPI's own validation errors arrive as a list, ours as a string.
    if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : 'Please check the form and try again.');
    f.reset();
    status.textContent = 'Thanks — your message was sent.';
    track('contact-sent', 'Contact form sent');
  } catch (err) {
    status.textContent = err.message;
  } finally {
    btn.disabled = false;
    mountCap('cap-slot');
  }
});

/* ── resume match ──────────────────────────────────────────────────────── */
// The panel stays hidden unless the server says matching is configured. The
// widget is only fetched once someone starts using the form.
let matchCfg = null;
let matchReady = null;
let pasteMode = false;

async function initMatch() {
  try {
    matchCfg = await api('/api/match');
    if (!matchCfg.enabled) return;
    $('match-window').textContent = `${matchCfg.window_days} days`;
    $('panel-match').hidden = false;
  } catch { /* matching is optional */ }
}

function prepareMatch() {
  matchReady ||= (async () => {
    try {
      await loadCap(matchCfg.endpoint);
      mountCap('match-cap-slot');
      return true;
    } catch {
      $('match-send').disabled = true;
      $('match-status').textContent = 'Resume matching is unavailable right now.';
      return false;
    }
  })();
  return matchReady;
}

const readBase64 = (file) => new Promise((resolve, reject) => {
  const fr = new FileReader();
  fr.onload = () => resolve(String(fr.result).split(',', 2)[1] || '');
  fr.onerror = () => reject(new Error('Could not read that file.'));
  fr.readAsDataURL(file);
});

function renderMatch(d) {
  const p = d.profile;
  const pretty = prettyRole;
  const yrs = p.years_experience ? ` · ~${Math.round(p.years_experience)} yrs` : '';
  $('match-profile').innerHTML =
    `Read as <b>${esc(p.seniority)}</b>${esc(yrs)}`
    + (p.roles.length ? ` · <b>${p.roles.map((r) => esc(pretty(r))).join(', ')}</b>` : '')
    + (p.skills.length ? `<br>${p.skills.map((k) => `<span class="pill">${esc(k)}</span>`).join('')}` : '');
  const tb = $('match-jobs').querySelector('tbody');
  tb.innerHTML = d.rows.length
    ? d.rows.map((r) => `<tr>${rowLead(r)}
        <td class="why" data-l="Why">${esc((r.why || []).join(', ')) || '—'}</td>
        ${rowActions(r)}</tr>`).join('')
    : '<tr><td colspan="7" class="empty">No recent listings matched. Try again after the next scrape.</td></tr>';
  $('match-results').hidden = false;
}

$('match-form').addEventListener('focusin', () => { if (matchCfg?.enabled) prepareMatch(); });
$('match-file').addEventListener('change', (e) => {
  const f = e.target.files[0];
  $('match-file-label').textContent = f ? f.name : 'Choose a PDF';
  $('match-status').textContent = f && f.size > matchCfg.max_pdf_bytes
    ? `That file is over ${Math.round(matchCfg.max_pdf_bytes / 1e6)} MB.` : '';
});
$('match-paste-toggle').addEventListener('click', () => {
  pasteMode = !pasteMode;
  $('match-text').hidden = !pasteMode;
  $('match-file').closest('.file-pick').hidden = pasteMode;
  $('match-paste-toggle').textContent = pasteMode ? 'or upload a PDF instead' : 'or paste the text instead';
  if (pasteMode) $('match-text').focus();
});

$('match-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const status = $('match-status');
  const btn = $('match-send');
  const file = $('match-file').files[0];
  const text = $('match-text').value.trim();
  if (pasteMode ? text.length < 200 : !file) {
    status.textContent = pasteMode ? 'Paste the whole resume (at least a few lines).' : 'Choose a PDF first.';
    return;
  }
  if (!pasteMode && file.size > matchCfg.max_pdf_bytes) {
    status.textContent = `That file is over ${Math.round(matchCfg.max_pdf_bytes / 1e6)} MB.`;
    return;
  }
  if (!(await prepareMatch())) return;

  btn.disabled = true;
  try {
    const token = await capToken('match-cap-slot', status);
    status.textContent = 'Reading your resume…';
    const body = { token, remote: $('match-remote').value, country: state.country };
    if (pasteMode) body.text = text; else body.pdf = await readBase64(file);
    const r = await fetch('/api/match', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : 'Could not match that resume.');
    renderMatch(d);
    status.textContent = `${plural(d.rows.length, 'match', 'matches')}, best first.`;
    track('match-sent', 'Resume matched');
  } catch (err) {
    // fetch rejects with a TypeError when the connection drops, which is also
    // how a busy server's refusal can arrive: it answers without reading the upload.
    status.textContent = err instanceof TypeError
      ? 'Resume matching is busy or unreachable right now. Please try again in a minute.'
      : err.message;
  } finally {
    btn.disabled = false;
    mountCap('match-cap-slot');
  }
});

// Loaded after the page is up, and only when GOATCOUNTER_HOST is set. The
// script counts the page view itself on load, and skips localhost and private
// addresses on its own, so dev traffic never reaches the stats.
async function loadAnalytics() {
  try {
    const { analytics } = await api('/api/site');
    if (!analytics) return;
    const el = document.createElement('script');
    el.async = true;
    el.src = '/vendor/goatcounter.js';
    el.dataset.goatcounter = analytics;
    document.head.appendChild(el);
  } catch { /* analytics is optional */ }
}

(async function init() {
  try {
    const { countries, default: def } = await api('/api/countries');
    if (!countries.some((c) => c.key === state.country)) state.country = def;
    $('country').innerHTML = countries.map((c) =>
      `<option value="${esc(c.key)}"${c.key === state.country ? ' selected' : ''}>${esc(c.label)}</option>`).join('');
  } catch {
    $('country').innerHTML = '<option value="india">India</option>';
  }
  reloadAll();
  initApplied({ track });
  initMatch();
  loadAnalytics();
})();
