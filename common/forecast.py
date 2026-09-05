"""Forecasting for the active-listings series.
Two models:

* ``linear``  - ols when sample size is small
* ``arima``   - non-seasonal ARIMA(p, d, q), estimated by the Hannan-Rissanen
                three-stage procedure and selected over a small (p, q) grid by
                AICc.  Used once enough history has accumulated.

Both return a point forecast plus an approximate 95% interval.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

Z95 = 1.959963984540054


@dataclass
class ForecastResult:
    model: str
    yhat: list[float]
    lo: list[float]
    hi: list[float]
    params: dict = field(default_factory=dict)

    def clipped(self, floor: float = 0.0) -> "ForecastResult":
        """Listing counts cannot go negative; clamp the whole fan at `floor`."""
        self.yhat = [max(floor, v) for v in self.yhat]
        self.lo = [max(floor, v) for v in self.lo]
        self.hi = [max(floor, v) for v in self.hi]
        return self


# --------------------------------------------------------------------------- #
# Linear regression
# --------------------------------------------------------------------------- #
def linear_forecast(y: np.ndarray, horizon: int, damping: float = 1.0) -> ForecastResult:
    """OLS of y on t, with a textbook prediction interval.

    se(y0) = s * sqrt(1 + 1/n + (x0 - xbar)^2 / Sxx)

    The leading 1 is what makes this a *prediction* interval (a new observation)
    rather than a confidence interval on the mean - the wider, honest one.

    With `damping` < 1 the slope decays geometrically past the end of the data,
    so the line bends toward a plateau instead of extrapolating forever.
    """
    n = len(y)
    x = np.arange(n, dtype=float)
    xbar = x.mean()
    sxx = float(((x - xbar) ** 2).sum()) or 1.0

    slope = float(((x - xbar) * (y - y.mean())).sum() / sxx)
    intercept = float(y.mean() - slope * xbar)

    resid = y - (intercept + slope * x)
    dof = max(n - 2, 1)
    s = float(math.sqrt(float((resid**2).sum()) / dof))

    fx = np.arange(n, n + horizon, dtype=float)
    if damping < 1.0:
        steps = np.arange(1, horizon + 1, dtype=float)
        decay = damping * (1.0 - damping**steps) / (1.0 - damping)  # sum of phi^1..phi^h
        yhat = (intercept + slope * (n - 1)) + slope * decay
    else:
        yhat = intercept + slope * fx
    se = s * np.sqrt(1.0 + 1.0 / n + (fx - xbar) ** 2 / sxx)

    return ForecastResult(
        model="linear",
        yhat=yhat.tolist(),
        lo=(yhat - Z95 * se).tolist(),
        hi=(yhat + Z95 * se).tolist(),
        params={"slope_per_day": slope, "intercept": intercept, "sigma": s,
                "n": n, "damping": damping},
    )


# --------------------------------------------------------------------------- #
# ARIMA helpers
# --------------------------------------------------------------------------- #
def _acf1(x: np.ndarray) -> float:
    """Lag-1 autocorrelation."""
    x = x - x.mean()
    denom = float((x**2).sum())
    if denom <= 0:
        return 0.0
    return float((x[1:] * x[:-1]).sum() / denom)


def _choose_d(y: np.ndarray, max_d: int = 2, threshold: float = 0.85) -> int:
    """Pick the differencing order.

    A proper unit-root test (ADF/KPSS) needs scipy for its p-value tables.  The
    practical stand-in: keep differencing while lag-1 autocorrelation stays near
    1, which is the signature of a random walk.  Stop early if the series gets
    too short or if differencing starts *inflating* variance (over-differencing).
    """
    d = 0
    cur = y.astype(float)
    while d < max_d and len(cur) > 10 and _acf1(cur) > threshold:
        nxt = np.diff(cur)
        if nxt.var() > cur.var():  # over-differencing - back off
            break
        cur = nxt
        d += 1
    return d


def _lagmat(x: np.ndarray, lags: int, start: int) -> np.ndarray:
    """Columns x[t-1], ..., x[t-lags] for t = start .. len(x)-1."""
    n = len(x)
    return np.column_stack([x[start - k : n - k] for k in range(1, lags + 1)]) if lags else np.empty((n - start, 0))


def _hannan_rissanen(w: np.ndarray, p: int, q: int):
    """Estimate ARMA(p, q) coefficients on a (already differenced) series.

    Stage 1: fit a long AR(m) by OLS to get a consistent estimate of the errors.
    Stage 2: regress w_t on its own lags and those estimated errors.

    Returns (const, phi, theta, sigma2, k_params, resid) or None if under-determined.
    """
    n = len(w)
    m = int(min(max(int(round(math.log(max(n, 3)) ** 2)), p + q + 1, 4), max(n // 4, 1)))
    if n - m < m + 5:
        return None

    # Stage 1 - long autoregression for the residual proxy.
    ar_x = np.column_stack([np.ones(n - m), _lagmat(w, m, m)])
    ar_y = w[m:]
    ar_beta, *_ = np.linalg.lstsq(ar_x, ar_y, rcond=None)
    eps = np.zeros(n)
    eps[m:] = ar_y - ar_x @ ar_beta

    # Stage 2 - regress on own lags + estimated error lags.
    start = max(p, q, m)
    if n - start < p + q + 3:
        return None
    cols = [np.ones(n - start)]
    if p:
        cols.append(_lagmat(w, p, start))
    if q:
        cols.append(_lagmat(eps, q, start))
    x = np.column_stack(cols)
    yv = w[start:]
    beta, *_ = np.linalg.lstsq(x, yv, rcond=None)

    const = float(beta[0])
    phi = np.asarray(beta[1 : 1 + p], dtype=float)
    theta = np.asarray(beta[1 + p : 1 + p + q], dtype=float)

    resid = yv - x @ beta
    k = 1 + p + q
    sigma2 = float((resid**2).sum() / max(len(resid) - k, 1))
    return const, phi, theta, sigma2, k, resid


def _aicc(sigma2: float, nobs: int, k: int) -> float:
    if sigma2 <= 0 or nobs <= k + 2:
        return math.inf
    aic = nobs * math.log(sigma2) + 2 * k
    return aic + (2 * k * (k + 1)) / (nobs - k - 1)


def _is_stationary(phi: np.ndarray) -> bool:
    """Roots of 1 - phi_1 z - ... - phi_p z^p outside the unit circle."""
    if phi.size == 0:
        return True
    companion = np.r_[1.0, -phi]
    roots = np.roots(companion[::-1])  # numpy wants highest power first
    return bool(np.all(np.abs(roots) > 1.0 + 1e-8)) if roots.size else True


def _psi_weights(phi: np.ndarray, theta: np.ndarray, d: int, h: int) -> np.ndarray:
    """MA(inf) weights of the *integrated* process, for the forecast variance.

    The full AR side is phi(B) * (1-B)^d; psi solves Phi(B) psi(B) = theta(B).
    """
    poly = np.r_[1.0, -phi] if phi.size else np.array([1.0])
    for _ in range(d):
        poly = np.convolve(poly, np.array([1.0, -1.0]))
    big_phi = -poly[1:]  # Phi_1 .. Phi_{p+d}

    psi = np.zeros(h)
    psi[0] = 1.0
    for j in range(1, h):
        acc = theta[j - 1] if j - 1 < theta.size else 0.0
        for i in range(1, min(j, big_phi.size) + 1):
            acc += big_phi[i - 1] * psi[j - i]
        psi[j] = acc
    return psi


def _simulate_forward(w: np.ndarray, resid: np.ndarray, const: float, phi: np.ndarray,
                      theta: np.ndarray, horizon: int, damping: float) -> np.ndarray:
    """Recursive point forecast of the differenced series (future shocks = 0)."""
    p, q = phi.size, theta.size
    hist = list(w[-p:]) if p else []
    errs = list(resid[-q:]) if q else []
    out = np.empty(horizon)
    for step in range(1, horizon + 1):
        # Damping shrinks the intercept (the drift term) geometrically, so a long
        # horizon flattens instead of running away on a straight line.
        val = const * (damping**step if damping < 1.0 else 1.0)
        for i in range(p):
            val += phi[i] * hist[-1 - i]
        for j in range(q):
            val += theta[j] * errs[-1 - j]
        out[step - 1] = val
        if p:
            hist.append(val)
        if q:
            errs.append(0.0)
    return out


def arima_forecast(y: np.ndarray, horizon: int, max_p: int = 3, max_q: int = 3,
                   max_d: int = 2, damping: float = 1.0) -> ForecastResult:
    """Non-seasonal ARIMA(p, d, q); order chosen by AICc over a small grid."""
    y = np.asarray(y, dtype=float)
    d = _choose_d(y, max_d=max_d)
    w = np.diff(y, n=d) if d else y.copy()

    best = None
    for p in range(max_p + 1):
        for q in range(max_q + 1):
            if p == 0 and q == 0 and d == 0:
                continue  # a bare mean is not a useful model here
            fit = _hannan_rissanen(w, p, q)
            if fit is None:
                continue
            const, phi, theta, sigma2, k, resid = fit
            if not _is_stationary(phi):
                continue
            score = _aicc(sigma2, len(resid), k)
            if not math.isfinite(score):
                continue
            if best is None or score < best[0]:
                best = (score, p, q, const, phi, theta, sigma2, resid)

    if best is None:  # grid came up empty - fall back rather than fail the run
        res = linear_forecast(y, horizon)
        res.params["fallback_from"] = "arima"
        return res

    score, p, q, const, phi, theta, sigma2, resid = best

    dw = _simulate_forward(w, resid, const, phi, theta, horizon, damping)

    # Undo the differencing to get back to levels.
    fc = dw
    for _ in range(d):
        fc = np.cumsum(fc)
    if d >= 1:
        tail = y[-1]
        if d == 2:
            fc = fc + (y[-1] - y[-2]) * np.arange(1, horizon + 1)
        fc = fc + tail

    psi = _psi_weights(phi, theta, d, horizon)
    var = sigma2 * np.cumsum(psi**2)
    se = np.sqrt(var)

    return ForecastResult(
        model=f"arima({p},{d},{q})",
        yhat=fc.tolist(),
        lo=(fc - Z95 * se).tolist(),
        hi=(fc + Z95 * se).tolist(),
        params={
            "p": p, "d": d, "q": q,
            "aicc": score,
            "const": const,
            "phi": phi.tolist(),
            "theta": theta.tolist(),
            "sigma2": sigma2,
            "damping": damping,
            "n": len(y),
        },
    )



def forecast_series(
    dates: list[str],
    values: list[float],
    until: str,
    *,
    arima_min_points: int = 30,
    min_points: int = 7,
    log_space: bool = True,
    damping: float = 1.0,
) -> dict | None:

    from datetime import date, timedelta

    if len(values) < min_points:
        return None

    last = date.fromisoformat(dates[-1])
    end = date.fromisoformat(until)
    horizon = (end - last).days
    if horizon <= 0:
        return None

    y = np.asarray(values, dtype=float)
    use_arima = len(values) >= arima_min_points
    log_space = log_space and use_arima  # straight lines stay straight

    work = np.log1p(np.clip(y, 0, None)) if log_space else y
    res = arima_forecast(work, horizon, damping=damping) if use_arima \
        else linear_forecast(work, horizon, damping=damping)

    if log_space:
        res.yhat = np.expm1(np.asarray(res.yhat)).tolist()
        res.lo = np.expm1(np.asarray(res.lo)).tolist()
        res.hi = np.expm1(np.asarray(res.hi)).tolist()
        res.params["space"] = "log1p"
    res.clipped(0.0)

    points = [
        {
            "date": (last + timedelta(days=i + 1)).isoformat(),
            "yhat": round(res.yhat[i], 2),
            "lo": round(res.lo[i], 2),
            "hi": round(res.hi[i], 2),
        }
        for i in range(horizon)
    ]
    return {
        "model": res.model,
        "fitted_on": {"start": dates[0], "end": dates[-1], "points": len(values)},
        "horizon_days": horizon,
        "params": {k: v for k, v in res.params.items()},
        "points": points,
    }
