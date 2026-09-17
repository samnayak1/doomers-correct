/* Time-series chart: observed line, damped forecast, uncertainty band.
   Built on Chart.js 4 (vendored in /vendor, not a CDN — the page stays
   self-contained and makes no third-party request).

   Two things differ from the hand-rolled SVG this replaces:
   canvas cannot resolve `var(--token)`, so theme colours are read from
   getComputedStyle at draw time; and the chart instance must be destroyed
   before redrawing or Chart.js leaks canvases on every country switch. */

const DAY = 86400000;
const toDay = (iso) => Math.round(Date.parse(`${iso}T00:00:00Z`) / DAY);
const fromDay = (d) => new Date(d * DAY);
const fmtInt = (n) => Math.round(n).toLocaleString('en-US');
const fmtDate = (d) => fromDay(d).toLocaleDateString('en-US',
  { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });

const token = (name) =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim();

/** Month-start ticks, thinned so labels never collide. */
function monthTicks(d0, d1, maxTicks) {
  const out = [];
  const c = fromDay(d0);
  c.setUTCDate(1);
  if (c.getTime() / DAY < d0) c.setUTCMonth(c.getUTCMonth() + 1);
  while (c.getTime() / DAY <= d1) {
    out.push(Math.round(c.getTime() / DAY));
    c.setUTCMonth(c.getUTCMonth() + 1);
  }
  const stride = Math.max(1, Math.ceil(out.length / maxTicks));
  return out.filter((_, i) => i % stride === 0);
}

/** Shade the stretch reconstructed from posting dates, before the first scrape. */
const reconstructedBand = {
  id: 'reconstructedBand',
  beforeDatasetsDraw(chart, _args, opts) {
    if (!opts || opts.until == null) return;
    const { ctx, chartArea: area, scales: { x } } = chart;
    const right = Math.min(x.getPixelForValue(opts.until), area.right);
    if (right <= area.left) return;
    ctx.save();
    ctx.fillStyle = token('--recon') || 'rgba(0,0,0,.06)';
    ctx.fillRect(area.left, area.top, right - area.left, area.bottom - area.top);
    ctx.fillStyle = token('--text-muted') || '#888';
    ctx.font = '10.5px system-ui, sans-serif';
    if (right - area.left > 110) ctx.fillText('RECONSTRUCTED', area.left + 7, area.top + 14);
    ctx.restore();
  },
};

/** One direct label at the end of the horizon — never a number on every point. */
const endLabel = {
  id: 'endLabel',
  afterDatasetsDraw(chart, _args, opts) {
    if (!opts || !opts.text || chart.width < 560) return;
    const { ctx, chartArea: area } = chart;
    ctx.save();
    ctx.fillStyle = token('--text-secondary') || '#555';
    ctx.font = '600 11.5px system-ui, sans-serif';
    ctx.textAlign = 'right';
    ctx.fillText(opts.text, area.right - 2, Math.max(area.top + 11, opts.y - 8));
    ctx.restore();
  },
};

export function drawChart(host, data) {
  const { history = [], forecast = [], observedFrom = null } = data;

  if (host._chart) { host._chart.destroy(); host._chart = null; }
  host.textContent = '';

  if (!history.length) {
    const p = document.createElement('div');
    p.className = 'empty';
    p.textContent = 'No data yet — the first scrape has not completed.';
    host.appendChild(p);
    return { hasReconstructed: false };
  }

  const canvas = document.createElement('canvas');
  canvas.style.width = '100%';
  canvas.height = (host.clientWidth || 900) < 560 ? 300 : 380;
  host.appendChild(canvas);

  const hist = history.map((d) => ({ x: toDay(d.date), y: +d.value }));
  const fc = forecast.map((d) => ({ x: toDay(d.date), y: +d.yhat, lo: +d.lo, hi: +d.hi }));
  const x0 = hist[0].x;
  const x1 = (fc.length ? fc[fc.length - 1] : hist[hist.length - 1]).x;

  // Everything left of the first completed scrape is reconstructed. Before any
  // scrape finishes there is no observed data at all, so the whole plot is.
  const reconUntil = observedFrom ? toDay(observedFrom) : hist[hist.length - 1].x;
  const hasReconstructed = reconUntil > x0;

  const s1 = token('--series-1') || '#0a84c2';
  const s2 = token('--series-2') || '#e8590c';
  const band = token('--band') || 'rgba(232,89,12,.15)';
  const grid = token('--border') || '#dcdfe3';
  const ink = token('--text-muted') || '#8b949e';
  const surface = token('--surface-1') || '#fff';

  // The forecast line starts at the last observed point so there is no visual gap.
  const joined = fc.length
    ? [{ x: hist[hist.length - 1].x, y: hist[hist.length - 1].y }, ...fc.map((p) => ({ x: p.x, y: p.y }))]
    : [];

  const datasets = [];
  if (fc.length) {
    datasets.push(
      { label: '_lo', data: fc.map((p) => ({ x: p.x, y: p.lo })), borderWidth: 0,
        pointRadius: 0, fill: false, order: 4 },
      { label: '_hi', data: fc.map((p) => ({ x: p.x, y: p.hi })), borderWidth: 0,
        pointRadius: 0, fill: '-1', backgroundColor: band, order: 4 },
    );
  }
  datasets.push({
    label: 'Observed', data: hist, borderColor: s1, borderWidth: 2,
    pointRadius: 0, pointHoverRadius: 4.5, pointHoverBackgroundColor: s1,
    pointHoverBorderColor: surface, pointHoverBorderWidth: 2,
    tension: 0, fill: false, order: 1,
  });
  if (joined.length) {
    datasets.push({
      label: 'Forecast', data: joined, borderColor: s2, borderWidth: 2,
      borderDash: [6, 5], pointRadius: 0, pointHoverRadius: 4.5,
      pointHoverBackgroundColor: s2, pointHoverBorderColor: surface,
      pointHoverBorderWidth: 2, tension: 0, fill: false, order: 2,
    });
  }

  const byDay = new Map(fc.map((p) => [p.x, p]));
  const last = fc.length ? fc[fc.length - 1] : null;

  host._chart = new Chart(canvas, {
    type: 'line',
    data: { datasets },
    plugins: [reconstructedBand, endLabel],
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      parsing: false,           // data is already {x, y}
      normalized: true,
      interaction: { mode: 'index', axis: 'x', intersect: false },
      layout: { padding: { right: 6, top: 4 } },
      scales: {
        x: {
          type: 'linear', min: x0, max: x1,
          grid: { color: grid, drawTicks: false },
          border: { color: grid },
          ticks: {
            color: ink, font: { size: 11 }, maxRotation: 0, autoSkip: false,
            padding: 8,
            callback: (v) => {
              const dt = fromDay(v);
              return dt.getUTCMonth() === 0
                ? String(dt.getUTCFullYear())
                : dt.toLocaleDateString('en-US', { month: 'short', timeZone: 'UTC' });
            },
          },
          afterBuildTicks: (axis) => {
            axis.ticks = monthTicks(x0, x1, canvas.clientWidth < 560 ? 4 : 8)
              .map((value) => ({ value }));
          },
        },
        y: {
          beginAtZero: true,
          grid: { color: grid, drawTicks: false },
          border: { display: false },
          ticks: {
            color: ink, font: { size: 11 }, padding: 8, maxTicksLimit: 8,
            callback: (v) => (v >= 1000 ? `${(v / 1000).toFixed(v % 1000 ? 1 : 0)}k` : v),
          },
        },
      },
      plugins: {
        legend: { display: false },     // the page renders its own HTML legend
        tooltip: {
          backgroundColor: surface,
          titleColor: token('--text-primary') || '#24292f',
          bodyColor: token('--text-secondary') || '#57606a',
          borderColor: token('--border-strong') || '#b9c0c8',
          borderWidth: 1, cornerRadius: 4, padding: 10, displayColors: true,
          usePointStyle: true,
          filter: (item) => !item.dataset.label.startsWith('_'),
          callbacks: {
            title: (items) => (items.length ? fmtDate(items[0].parsed.x) : ''),
            label: (item) => ` ${item.dataset.label}: ${fmtInt(item.parsed.y)}`,
            afterBody: (items) => {
              const p = items.length ? byDay.get(items[0].parsed.x) : null;
              return p ? `95% range: ${fmtInt(p.lo)}–${fmtInt(p.hi)}` : '';
            },
          },
        },
        reconstructedBand: { until: hasReconstructed ? reconUntil : null },
        endLabel: last
          ? { text: `${fmtInt(last.y)} · ${last.x && fromDay(last.x).toISOString().slice(0, 7)}`, y: 0 }
          : {},
      },
    },
  });

  // Position the end label against the top of the band once scales exist.
  if (last) {
    const yScale = host._chart.scales.y;
    host._chart.options.plugins.endLabel.y = yScale.getPixelForValue(Math.min(last.hi, yScale.max));
    host._chart.update('none');
  }

  return { hasReconstructed };
}
