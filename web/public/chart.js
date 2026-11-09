
//CODED BY AI. 
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

/** Round up to a 1 / 2 / 2.5 / 5 x 10^k step so the top gridline is a clean number. */
function niceCeil(v) {
  if (!(v > 0)) return 10;
  const mag = 10 ** Math.floor(Math.log10(v));
  return [1, 2, 2.5, 5, 10].map((m) => m * mag).find((n) => n >= v);
}

const prettyRole = (r) => ({ aiml: 'AI / ML', ui_ux: 'UI / UX', qa: 'QA', sre: 'SRE', it_support: 'IT support' }[r]
  || r.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase()));
export { prettyRole };

// Role lines are faint until pointed at, so the total stays the subject.
const FAINT_ALPHA = 0.4;
const withAlpha = (hex, a) => {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${n >> 16}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
};

/** Paint one role line bold (or none, with null) and the rest faint. */
function setHighlight(chart, role) {
  if (!chart || chart.$hl === role) return;
  chart.$hl = role;
  for (const ds of chart.data.datasets) {
    if (!ds.$role) continue;
    const on = ds.$role === role;
    ds.borderColor = on ? ds.$color : withAlpha(ds.$color, FAINT_ALPHA);
    ds.borderWidth = on ? 2.75 : 1.25;
    ds.order = on ? 0 : 3;     // the bold line draws over the observed series
  }
  chart.update('none');
  chart.$onHighlight?.(role);
}

/** Exposed so the HTML legend can drive the same highlight as the canvas.
 *  A pinned role (a tapped legend entry) is what the chart falls back to when
 *  nothing is being pointed at - touch screens have no hover to hold it. */
export function highlightRole(host, role) {
  const c = host._chart;
  if (c) setHighlight(c, role ?? c.$pinned ?? null);
}
export function pinRole(host, role) {
  const c = host._chart;
  if (!c) return;
  c.$pinned = role;
  setHighlight(c, role);
}

/** Names the bold role line at its latest point - a label, not colour alone. */
const roleLabel = {
  id: 'roleLabel',
  afterDatasetsDraw(chart) {
    const ds = chart.data.datasets.find((d) => d.$role && d.$role === chart.$hl);
    const last = ds && ds.data[ds.data.length - 1];
    if (!last) return;
    const { ctx, scales: { x, y }, chartArea: area } = chart;
    const px = x.getPixelForValue(last.x);
    const py = y.getPixelForValue(last.y);
    const text = `${prettyRole(ds.$role)} · ${fmtInt(last.y)}`;
    ctx.save();
    ctx.font = '600 12px system-ui, sans-serif';
    const w = ctx.measureText(text).width;
    // Right of the line's end when there is room, otherwise left of it.
    const right = px + 8 + w <= area.right;
    const tx = right ? px + 8 : px - 8 - w;
    const ty = Math.min(Math.max(py - 8, area.top + 12), area.bottom - 4);
    ctx.fillStyle = token('--surface-1') || '#fff';
    ctx.fillRect(tx - 3, ty - 12, w + 6, 16);
    ctx.fillStyle = token('--text-primary') || '#24292f';
    ctx.fillText(text, tx, ty);
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
  const { history = [], forecast = [], roles = [] } = data;

  if (host._chart) { host._chart.destroy(); host._chart = null; }
  host.textContent = '';

  if (!history.length) {
    const p = document.createElement('div');
    p.className = 'empty';
    p.textContent = 'No data yet — the first scrape has not completed.';
    host.appendChild(p);
    return;
  }

  const canvas = document.createElement('canvas');
  canvas.style.width = '100%';
  canvas.height = (host.clientWidth || 900) < 560 ? 300 : 380;
  host.appendChild(canvas);

  const hist = history.map((d) => ({ x: toDay(d.date), y: +d.value }));
  const fc = forecast.map((d) => ({ x: toDay(d.date), y: +d.yhat, lo: +d.lo, hi: +d.hi }));
  const x0 = hist[0].x;
  const x1 = (fc.length ? fc[fc.length - 1] : hist[hist.length - 1]).x;

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
  // One faint line per role. They start at the first night that recorded role
  // counts, so they sit inside the existing x-range rather than widening it.
  roles.forEach((r, i) => {
    const pts = r.points.map((p) => ({ x: toDay(p.date), y: +p.value }))
      .filter((p) => p.x >= x0 && p.x <= x1);
    if (!pts.length) return;
    const color = token(`--role-${i + 1}`) || '#888888';
    datasets.push({
      label: prettyRole(r.role), data: pts, $role: r.role, $color: color,
      borderColor: withAlpha(color, FAINT_ALPHA), borderWidth: 1.25,
      // A single night is a single point: draw it, or the role is invisible.
      pointRadius: pts.length === 1 ? 2.5 : 0, pointBackgroundColor: color,
      pointHoverRadius: 0, cubicInterpolationMode: 'monotone', fill: false, order: 3,
    });
  });

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

  // Scale to the observed series and the forecast line, never to the band. A
  // log-space ARIMA interval widens exponentially with the horizon - 14 months
  // out its upper edge was 200x the data, which flattened everything real into
  // the x-axis. The band is clipped at the top instead; the tooltip still
  // reports the true range, and the caller is told so the legend can say so.
  const yTop = niceCeil(1.25 * Math.max(...hist.map((p) => p.y), ...fc.map((p) => p.y)));
  const bandClipped = fc.some((p) => p.hi > yTop);

  const byDay = new Map(fc.map((p) => [p.x, p]));
  const last = fc.length ? fc[fc.length - 1] : null;

  host._chart = new Chart(canvas, {
    type: 'line',
    data: { datasets },
    plugins: [endLabel, roleLabel],
    options: {
      // The nearest role line within reach of the pointer turns bold. Distance
      // is measured to the line itself, interpolated between its daily points,
      // so the pointer has to be near the line rather than on one of its dots.
      onHover: (evt, _active, chart) => {
        // Chart.js reports leaving the canvas as one last hover at the exit
        // point, which would re-select whatever line the pointer crossed.
        if (evt.type === 'mouseout' || chart.$out) { setHighlight(chart, chart.$pinned ?? null); return; }
        const { x: xs, y: ys } = chart.scales;
        const day = xs.getValueForPixel(evt.x);
        let best = null; let bestD = 14;   // px - bigger than the 1.25px mark
        for (const ds of chart.data.datasets) {
          if (!ds.$role) continue;
          const pts = ds.data;
          let yv = null;
          if (pts.length === 1) {
            if (Math.abs(xs.getPixelForValue(pts[0].x) - evt.x) <= bestD) yv = pts[0].y;
          } else {
            for (let k = 1; k < pts.length; k += 1) {
              if (day >= pts[k - 1].x && day <= pts[k].x) {
                const t = (day - pts[k - 1].x) / ((pts[k].x - pts[k - 1].x) || 1);
                yv = pts[k - 1].y + t * (pts[k].y - pts[k - 1].y);
                break;
              }
            }
          }
          if (yv == null) continue;
          const d = Math.abs(ys.getPixelForValue(yv) - evt.y);
          if (d < bestD) { bestD = d; best = ds.$role; }
        }
        setHighlight(chart, best ?? chart.$pinned ?? null);
      },
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      parsing: false,           // data is already {x, y}
      normalized: true,
      // 'nearest' along x returns every series with a point on the hovered
      // day. 'index' paired the Nth point of each series instead, which was
      // only right while every series started on the same day - the role
      // lines and the forecast do not, so it showed values from other dates.
      interaction: { mode: 'nearest', axis: 'x', intersect: false },
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
          max: yTop,
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
          // Role rows only for the bold line - six more rows on every hover
          // would bury the total the chart is about.
          filter: (item) => !item.dataset.label.startsWith('_')
            && (!item.dataset.$role || item.dataset.$role === item.chart.$hl),
          callbacks: {
            title: (items) => (items.length ? fmtDate(items[0].parsed.x) : ''),
            label: (item) => ` ${item.dataset.label}: ${fmtInt(item.parsed.y)}`,
            afterBody: (items) => {
              const p = items.length ? byDay.get(items[0].parsed.x) : null;
              return p ? `95% range: ${fmtInt(p.lo)}–${fmtInt(p.hi)}` : '';
            },
          },
        },
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
  // Chart.js handles mouse moves on the next animation frame, so after a fast
  // exit its last in-canvas move arrives AFTER the pointer has left and would
  // re-select a line. `$out` makes that late move a no-op.
  canvas.addEventListener('mouseleave', () => {
    const c = host._chart;
    if (!c) return;
    c.$out = true;
    setHighlight(c, c.$pinned ?? null);
  });
  canvas.addEventListener('mouseenter', () => { if (host._chart) host._chart.$out = false; });
  return { bandClipped, roles: datasets.filter((d) => d.$role).map((d) => ({ role: d.$role, color: d.$color })) };
}
