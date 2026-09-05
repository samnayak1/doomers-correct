/* Hand-rolled SVG time-series chart: observed line + damped forecast with an
   uncertainty band. No dependencies — the box this runs on has 1 GB of RAM and
   a charting bundle is not worth the bytes.

   Conventions: 2px marks, recessive grid, colour carried by the mark only
   (all text uses text tokens), crosshair + tooltip on hover, legend always
   present because there is more than one series. */

const NS = 'http://www.w3.org/2000/svg';
const DAY = 86400000;

const el = (name, attrs = {}) => {
  const n = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined) n.setAttribute(k, v);
  return n;
};
const toDay = (iso) => Math.round(Date.parse(iso + 'T00:00:00Z') / DAY);
const fromDay = (d) => new Date(d * DAY);
const fmtInt = (n) => Math.round(n).toLocaleString('en-US');
const fmtDate = (iso) => fromDay(toDay(iso)).toLocaleDateString('en-US',
  { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });

/** "Nice" axis ticks covering [0, max].
    Picks the candidate step that wastes the least headroom while keeping the
    tick count in a readable range - a plain 1/2/5 ramp routinely rounds 16.4k
    up to a 20k axis and throws away a fifth of the plot height. */
function niceTicks(max, minTicks = 4, maxTicks = 8) {
  if (!(max > 0)) return { top: 1, ticks: [0, 1] };
  const mag = Math.pow(10, Math.floor(Math.log10(max)) - 1);
  let best = null;
  for (const m of [1, 1.5, 2, 2.5, 3, 4, 5, 6, 7.5, 10, 12.5, 15, 20, 25, 50, 100]) {
    const step = m * mag;
    const n = Math.ceil(max / step);
    if (n < minTicks || n > maxTicks) continue;
    const top = n * step;
    if (!best || top < best.top) best = { top, step };
  }
  if (!best) {
    const step = Math.pow(10, Math.ceil(Math.log10(max / maxTicks)));
    best = { step, top: Math.ceil(max / step) * step };
  }
  const ticks = [];
  for (let v = 0; v <= best.top + best.step / 2; v += best.step) ticks.push(v);
  return { top: best.top, ticks };
}

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

/** Keep at most `budget` points per line; the eye cannot resolve more than one
    per pixel and the DOM should not carry them. */
function thin(points, budget) {
  if (points.length <= budget) return points;
  const stride = Math.ceil(points.length / budget);
  const out = points.filter((_, i) => i % stride === 0);
  if (out[out.length - 1] !== points[points.length - 1]) out.push(points[points.length - 1]);
  return out;
}

export function drawChart(host, data) {
  const { history = [], forecast = [], observedFrom = null, unit = 'listings' } = data;
  host.textContent = '';
  if (!history.length) {
    const p = document.createElement('div');
    p.className = 'empty';
    p.textContent = 'No data yet — the first scrape has not completed.';
    host.appendChild(p);
    return { hasReconstructed: false };
  }

  const W = Math.max(320, host.clientWidth || 900);
  const H = W < 560 ? 300 : 400;
  const M = { t: 14, r: 14, b: 34, l: 60 };
  const iw = W - M.l - M.r;
  const ih = H - M.t - M.b;

  const hist = history.map((d) => ({ x: toDay(d.date), y: +d.value, date: d.date }));
  const fc = forecast.map((d) => ({ x: toDay(d.date), y: +d.yhat, lo: +d.lo, hi: +d.hi, date: d.date }));

  const x0 = hist[0].x;
  const x1 = (fc.length ? fc[fc.length - 1] : hist[hist.length - 1]).x;
  const yMaxRaw = Math.max(...hist.map((d) => d.y), ...fc.map((d) => d.hi), 1);
  const { top: yTop, ticks: yTicks } = niceTicks(yMaxRaw * 1.02);

  const X = (v) => M.l + ((v - x0) / Math.max(1, x1 - x0)) * iw;
  const Y = (v) => M.t + ih - (v / yTop) * ih;

  const svg = el('svg', {
    viewBox: `0 0 ${W} ${H}`, width: W, height: H,
    role: 'img', 'aria-label': `Active tech listings over time with a forecast to ${fc.length ? fc[fc.length - 1].date : 'today'}`,
  });

  // ── shaded region: days reconstructed from posting dates, before we started
  //    collecting. Survivorship-biased, and never fed to the model.
  // Everything left of the first completed scrape is reconstructed from posting
  // dates. Before *any* scrape has finished there is no observed data at all, so
  // the entire curve is reconstructed and the whole plot gets shaded - that is
  // the state a fresh deployment sits in, and it is the most misleading part of
  // the chart precisely when it is least labelled.
  const reconUntil = observedFrom ? toDay(observedFrom) : hist[hist.length - 1].x;
  const hasReconstructed = reconUntil > x0;
  if (hasReconstructed) {
    const xr = X(reconUntil);
    svg.appendChild(el('rect', { x: M.l, y: M.t, width: Math.max(0, xr - M.l), height: ih, fill: 'var(--recon)' }));
    if (xr - M.l > 62) {
      const t = el('text', {
        x: M.l + 7, y: M.t + 14, fill: 'var(--text-muted)',
        'font-size': 10.5, 'letter-spacing': '.04em',
      });
      t.textContent = 'RECONSTRUCTED';
      svg.appendChild(t);
    }
  }

  // ── grid + y axis
  for (const v of yTicks) {
    const y = Math.round(Y(v)) + 0.5;
    svg.appendChild(el('line', { x1: M.l, x2: W - M.r, y1: y, y2: y, stroke: 'var(--border)', 'stroke-width': 1 }));
    const t = el('text', { x: M.l - 9, y: y + 4, 'text-anchor': 'end', fill: 'var(--text-muted)', 'font-size': 11 });
    t.textContent = v >= 1000 ? (v / 1000).toFixed(v % 1000 ? 1 : 0) + 'k' : String(v);
    svg.appendChild(t);
  }

  // ── x axis
  for (const d of monthTicks(x0, x1, W < 560 ? 4 : 8)) {
    const x = Math.round(X(d)) + 0.5;
    svg.appendChild(el('line', { x1: x, x2: x, y1: M.t, y2: M.t + ih, stroke: 'var(--border)', 'stroke-width': 1, opacity: 0.55 }));
    const t = el('text', { x, y: H - 12, 'text-anchor': 'middle', fill: 'var(--text-muted)', 'font-size': 11 });
    const dt = fromDay(d);
    t.textContent = dt.getUTCMonth() === 0
      ? String(dt.getUTCFullYear())
      : dt.toLocaleDateString('en-US', { month: 'short', timeZone: 'UTC' });
    svg.appendChild(t);
  }

  const line = (pts, key) => pts.map((p, i) => `${i ? 'L' : 'M'}${X(p.x).toFixed(1)},${Y(p[key]).toFixed(1)}`).join('');

  // ── forecast uncertainty band (drawn under the lines)
  if (fc.length > 1) {
    const band = thin(fc, 400);
    const up = band.map((p) => `${X(p.x).toFixed(1)},${Y(Math.min(p.hi, yTop)).toFixed(1)}`);
    const dn = band.slice().reverse().map((p) => `${X(p.x).toFixed(1)},${Y(Math.max(p.lo, 0)).toFixed(1)}`);
    svg.appendChild(el('polygon', { points: [...up, ...dn].join(' '), fill: 'var(--band)' }));
  }

  // ── observed series
  const histThin = thin(hist, Math.max(240, Math.floor(iw)));
  svg.appendChild(el('path', {
    d: line(histThin, 'y'), fill: 'none', stroke: 'var(--series-1)',
    'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round',
  }));

  // ── forecast series, joined to the last observed point so there is no gap
  if (fc.length) {
    const joined = [{ x: hist[hist.length - 1].x, y: hist[hist.length - 1].y }, ...thin(fc, 400)];
    svg.appendChild(el('path', {
      d: line(joined, 'y'), fill: 'none', stroke: 'var(--series-2)',
      'stroke-width': 2, 'stroke-dasharray': '6 5', 'stroke-linecap': 'round',
    }));

    // "today" divider
    const xd = Math.round(X(hist[hist.length - 1].x)) + 0.5;
    svg.appendChild(el('line', {
      x1: xd, x2: xd, y1: M.t, y2: M.t + ih, stroke: 'var(--border-strong)',
      'stroke-width': 1, 'stroke-dasharray': '3 3',
    }));

    // One selective direct label: the value at the end of the horizon, sitting
    // above the band so it never lands on top of a mark. Dropped on narrow
    // viewports, where there is no room for it to clear anything.
    if (W >= 560) {
      const last = fc[fc.length - 1];
      const ly = Math.max(M.t + 11, Y(Math.min(last.hi, yTop)) - 8);
      const lab = el('text', {
        x: W - M.r - 2, y: ly, 'text-anchor': 'end',
        fill: 'var(--text-secondary)', 'font-size': 11.5, 'font-weight': 600,
      });
      lab.textContent = `${fmtInt(last.y)} · ${last.date.slice(0, 7)}`;
      svg.appendChild(lab);
    }
  }

  // ── axis frame (baseline only; the rest is grid)
  svg.appendChild(el('line', {
    x1: M.l, x2: W - M.r, y1: M.t + ih + 0.5, y2: M.t + ih + 0.5,
    stroke: 'var(--border-strong)', 'stroke-width': 1,
  }));

  // ── hover layer: crosshair, dots on each series, tooltip
  const cross = el('line', { y1: M.t, y2: M.t + ih, stroke: 'var(--border-strong)', 'stroke-width': 1, opacity: 0 });
  const dotH = el('circle', { r: 4.5, fill: 'var(--series-1)', stroke: 'var(--surface-1)', 'stroke-width': 2, opacity: 0 });
  const dotF = el('circle', { r: 4.5, fill: 'var(--series-2)', stroke: 'var(--surface-1)', 'stroke-width': 2, opacity: 0 });
  svg.append(cross, dotH, dotF);

  const tip = document.createElement('div');
  tip.className = 'tip';
  host.appendChild(tip);

  const byDay = new Map();
  for (const p of hist) byDay.set(p.x, { ...byDay.get(p.x), hist: p });
  for (const p of fc) byDay.set(p.x, { ...byDay.get(p.x), fc: p });

  const hit = el('rect', { x: M.l, y: M.t, width: iw, height: ih, fill: 'transparent', style: 'cursor:crosshair' });
  svg.appendChild(hit);

  const hide = () => {
    tip.style.opacity = 0;
    for (const n of [cross, dotH, dotF]) n.setAttribute('opacity', 0);
  };

  const move = (evt) => {
    const box = svg.getBoundingClientRect();
    const scale = W / box.width;
    const px = (evt.clientX - box.left) * scale;
    const day = Math.round(x0 + ((px - M.l) / iw) * (x1 - x0));
    let rec = byDay.get(day);
    if (!rec) {                              // snap to the nearest day we have
      let best = null, bd = Infinity;
      for (const k of byDay.keys()) {
        const dd = Math.abs(k - day);
        if (dd < bd) { bd = dd; best = k; }
      }
      if (best === null) return;
      rec = byDay.get(best);
    }
    const anchor = rec.hist || rec.fc;
    const cx = X(anchor.x);
    cross.setAttribute('x1', cx); cross.setAttribute('x2', cx); cross.setAttribute('opacity', 0.8);

    const rows = [];
    if (rec.hist) {
      dotH.setAttribute('cx', cx); dotH.setAttribute('cy', Y(rec.hist.y)); dotH.setAttribute('opacity', 1);
      rows.push(`<div class="r"><em><i class="dot" style="background:var(--series-1)"></i>Observed</em><b>${fmtInt(rec.hist.y)}</b></div>`);
    } else dotH.setAttribute('opacity', 0);
    if (rec.fc) {
      dotF.setAttribute('cx', cx); dotF.setAttribute('cy', Y(rec.fc.y)); dotF.setAttribute('opacity', 1);
      rows.push(`<div class="r"><em><i class="dot" style="background:var(--series-2)"></i>Forecast</em><b>${fmtInt(rec.fc.y)}</b></div>`);
      rows.push(`<div class="r"><em>95% range</em><b>${fmtInt(rec.fc.lo)}–${fmtInt(rec.fc.hi)}</b></div>`);
    } else dotF.setAttribute('opacity', 0);

    tip.innerHTML = `<div class="d">${fmtDate(anchor.date)}</div>${rows.join('')}`;
    tip.style.opacity = 1;
    const hostW = host.clientWidth || W;
    const tipW = tip.offsetWidth || 170;
    const left = Math.min(Math.max(4, (cx / scale) + 14), hostW - tipW - 4);
    tip.style.left = `${left}px`;
    tip.style.top = `${Math.max(4, (M.t / scale) + 6)}px`;
  };

  hit.addEventListener('mousemove', move);
  hit.addEventListener('mouseleave', hide);
  hit.addEventListener('touchstart', (e) => { if (e.touches[0]) move(e.touches[0]); }, { passive: true });
  hit.addEventListener('touchmove', (e) => { if (e.touches[0]) move(e.touches[0]); }, { passive: true });

  host.appendChild(svg);
  host.appendChild(tip);
  return { hasReconstructed };
}
