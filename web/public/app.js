import { drawChart } from '/chart.js';

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
  tech: 1, q: '', site: '', remote: '', sort: 'date_posted',
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
  const sym = { INR: '₹', AUD: 'A$', USD: '$' }[currency] || '';
  const short = (n) => {
    if (n == null) return '';
    if (currency === 'INR' && n >= 100000) return `${(n / 100000).toFixed(n >= 1000000 ? 0 : 1)}L`;
    if (n >= 1000) return `${Math.round(n / 1000)}k`;
    return String(Math.round(n));
  };
  const body = lo && hi && Math.round(lo) !== Math.round(hi) ? `${short(lo)}–${short(hi)}` : short(hi || lo);
  return `${sym}${body}`;
}

/* ── theme ─────────────────────────────────────────────────────────────── */
(function theme() {
  const saved = (() => { try { return localStorage.getItem('theme'); } catch { return null; } })();
  if (saved) document.documentElement.setAttribute('data-theme', saved);
  $('theme').addEventListener('click', () => {
    const cur = document.documentElement.getAttribute('data-theme')
      || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    const next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem('theme', next); } catch { /* private mode */ }
    if (lastSeries) renderChart(lastSeries);   // re-read the CSS colour tokens
  });
})();

/* ── stat tiles ────────────────────────────────────────────────────────── */
function renderStats(d) {
  const s = d.summary || {};
  const fc = d.forecast;
  const delta = s.delta_pct;
  const endPoint = fc && fc.points.length ? fc.points[fc.points.length - 1] : null;
  const tiles = [
    {
      k: 'Active listings',
      v: nf.format(s.tech_active || 0),
      s: s.latest_scrape ? `as of ${fmtDate(s.latest_scrape)}` : 'first scrape still running',
    },
    {
      k: 'Change vs last run',
      v: delta == null ? '—' : `${delta > 0 ? '+' : ''}${delta.toFixed(1)}%`,
      s: plural(s.new_jobs || 0, 'new listing'),
      cls: delta == null ? '' : (delta >= 0 ? 'up' : 'down'),
    },
    {
      k: 'Remote',
      v: s.tech_active ? `${Math.round((s.remote_active / s.tech_active) * 100)}%` : '—',
      s: plural(s.remote_active || 0, 'listing'),
    },
    {
      k: 'Forecast · Dec 2027',
      v: endPoint ? nf.format(Math.round(endPoint.yhat)) : '—',
      s: fc ? fc.model : `needs ${d.config.arima_min_points}+ days`,
    },
  ];
  $('stats').innerHTML = tiles.map((t) => `
    <div class="stat">
      <div class="k">${t.k}</div>
      <div class="v ${t.cls || ''}">${t.v}</div>
      <div class="s">${t.s}</div>
    </div>`).join('');
}

/* ── chart ─────────────────────────────────────────────────────────────── */
function renderChart(d) {
  const s = d.series || { dates: [], values: [] };
  const history = s.dates.map((date, i) => ({ date, value: s.values[i] }));
  const drawn = drawChart($('chart'), {
    history,
    forecast: d.forecast ? d.forecast.points : [],
    observedFrom: s.observed_from,
  });
  // Legend entries only for marks that are actually on the chart.
  $('lg-forecast').hidden = !d.forecast;
  $('lg-band').hidden = !d.forecast;
  $('lg-recon').hidden = !(drawn && drawn.hasReconstructed);

  $('chart-sub').textContent = history.length
    ? `${fmtDate(history[0].date)} – ${fmtDate(history[history.length - 1].date)} · `
      + plural(s.observed_days || 0, 'scrape')
    : '';

  const fc = d.forecast;
  $('model-note').textContent = fc
    ? `Projection: ${fc.model}, fitted on ${plural(fc.fitted_on.points, 'observed day')} `
      + `(${fmtDate(fc.fitted_on.start)} – ${fmtDate(fc.fitted_on.end)}), extended ${plural(fc.horizon_days, 'day')} `
      + `to ${d.config.forecast_until}. Trend damping ${fc.params.damping ?? 1}. `
      + `This is an extrapolation of the current trend, not a prediction — see the method note below.`
    : `No forecast yet: the model needs at least ${d.config.forecast_min_points} days of collected `
      + `data (linear regression), and ${d.config.arima_min_points} days before it switches to ARIMA. `
      + (s.observed_from
          ? `Until then the chart shows collected history only.`
          : `Nothing has been collected yet, so the whole curve is reconstructed from posting dates `
            + `— shaded above, and understated the further back it goes.`);
}

/* ── jobs table ────────────────────────────────────────────────────────── */
function renderJobs(d) {
  state.total = d.total;
  state.currency = d.currency;
  const tbody = $('jobs').querySelector('tbody');
  if (!d.rows.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty">No listings match these filters.</td></tr>`;
  } else {
    tbody.innerHTML = d.rows.map((r) => {
      const row = { ...r, currency: d.currency };
      return `<tr>
        <td class="title">${esc(r.title)}</td>
        <td class="co">${esc(r.company)}</td>
        <td class="co">${esc(r.location)}</td>
        <td><span class="pill">${esc(r.site)}</span></td>
        <td class="num">${fmtDate(r.date_posted)}</td>
        <td class="num">${fmtSalary(row)}</td>
        <td>${r.is_remote ? '<span class="pill remote">Remote</span>' : ''}
            ${r.job_url ? `<a href="${esc(r.job_url)}" target="_blank" rel="noopener noreferrer nofollow">Open&nbsp;↗</a>` : ''}</td>
      </tr>`;
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
    const d = await api('/api/series', { country: state.country, tech: state.tech });
    lastSeries = d;
    $('hero-country').textContent = d.prose || d.label;
    $('chart-title').textContent = state.tech ? 'Active tech listings' : 'All active listings';
    $('freshness').textContent = d.summary.latest_scrape
      ? `Last scrape ${fmtDate(d.summary.latest_scrape)} · ${plural(d.summary.tracked_total, 'listing')} tracked`
      : d.summary.tracked_total
        ? `First scrape in progress · ${plural(d.summary.tracked_total, 'listing')} so far`
        : 'Awaiting first scrape';
    renderStats(d);
    renderChart(d);
    $('site').innerHTML = '<option value="">All sources</option>'
      + (d.summary.by_site || []).map((s) => `<option value="${esc(s.site)}">${esc(s.site)} (${nf.format(s.n)})</option>`).join('');
    $('site').value = state.site;
  } catch (err) {
    $('chart').innerHTML = `<div class="empty">Could not load the series — ${esc(err.message)}</div>`;
    $('stats').innerHTML = '';
  }
}

async function loadJobs() {
  try {
    renderJobs(await api('/api/jobs', {
      country: state.country, tech: state.tech, q: state.q, site: state.site,
      remote: state.remote, sort: state.sort, limit: state.limit, offset: state.offset,
    }));
  } catch (err) {
    $('jobs').querySelector('tbody').innerHTML =
      `<tr><td colspan="7" class="empty">Could not load listings — ${esc(err.message)}</td></tr>`;
  }
}

async function loadDataLinks() {
  try {
    const d = await api('/api/data');
    const p = $('data-links');
    if (!d.s3) { p.innerHTML = '<strong>Raw data.</strong> S3 publishing is not configured on this deployment.'; return; }
    const c = d.countries[state.country] || {};
    p.innerHTML = `<strong>Raw data.</strong> Everything here is public JSON: `
      + `<a href="${esc(d.manifest)}" target="_blank" rel="noopener">manifest</a>, `
      + `<a href="${esc(c.series)}" target="_blank" rel="noopener">series</a>, `
      + `<a href="${esc(c.latest)}" target="_blank" rel="noopener">current listings</a>. `
      + `Nightly snapshots live under <code>${esc(c.raw_prefix || '')}</code>.`;
  } catch { /* the section is optional */ }
}

/* ── wiring ────────────────────────────────────────────────────────────── */
function reloadAll() {
  state.offset = 0;
  const u = new URL(location);
  u.searchParams.set('country', state.country);
  history.replaceState(null, '', u);
  loadSeries(); loadJobs(); loadDataLinks();
}

$('country').addEventListener('change', (e) => { state.country = e.target.value; state.site = ''; reloadAll(); });
$('scope').addEventListener('change', (e) => { state.tech = +e.target.value; reloadAll(); });
$('site').addEventListener('change', (e) => { state.site = e.target.value; state.offset = 0; loadJobs(); });
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

(async function init() {
  try {
    const { countries, default: def } = await api('/api/countries');
    if (!countries.some((c) => c.key === state.country)) state.country = def;
    $('country').innerHTML = countries.map((c) =>
      `<option value="${esc(c.key)}"${c.key === state.country ? ' selected' : ''}>${esc(c.label)}</option>`).join('');
  } catch {
    $('country').innerHTML = '<option value="india">India</option><option value="australia">Australia</option>';
  }
  reloadAll();
})();
