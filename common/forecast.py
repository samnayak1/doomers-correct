
from __future__ import annotations

import warnings
from dataclasses import dataclass, field

import numpy as np


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


def _damp(last: float, yhat: np.ndarray, lo: np.ndarray, hi: np.ndarray, damping: float):
    """Decay each successive step of the forecast geometrically.

    Applied to the *increments* rather than the levels, so a model whose
    forecast is a constant drift converges to `last + slope * phi/(1-phi)` -
    about 49 further days of trend at phi=0.98, whatever horizon is asked for.
    The interval is shifted by the same amount rather than rescaled: damping is
    a judgement about the trend, not a claim that the model got more certain.
    """
    if damping >= 1.0:
        return yhat, lo, hi
    steps = np.arange(1, len(yhat) + 1)
    inc = np.diff(np.concatenate([[last], yhat]))
    damped = last + np.cumsum(inc * damping**steps)
    shift = damped - yhat
    return damped, lo + shift, hi + shift


def linear_forecast(y: np.ndarray, horizon: int, damping: float = 1.0) -> ForecastResult:
    """OLS of y on t, with a genuine prediction interval (a new observation,
    not the mean), which is the wider and honest one."""
    import statsmodels.api as sm

    y = np.asarray(y, dtype=float)
    n = len(y)
    x = sm.add_constant(np.arange(n, dtype=float))
    fit = sm.OLS(y, x).fit()

    fx = sm.add_constant(np.arange(n, n + horizon, dtype=float), has_constant="add")
    pred = fit.get_prediction(fx)
    yhat = np.asarray(pred.predicted_mean, dtype=float)
    ci = np.asarray(pred.conf_int(obs=True, alpha=0.05), dtype=float)
    lo, hi = ci[:, 0], ci[:, 1]

    yhat, lo, hi = _damp(float(y[-1]), yhat, lo, hi, damping)

    return ForecastResult(
        model="linear",
        yhat=yhat.tolist(), lo=lo.tolist(), hi=hi.tolist(),
        params={"slope_per_day": float(fit.params[1]), "intercept": float(fit.params[0]),
                "sigma": float(np.sqrt(fit.mse_resid)), "n": n, "damping": damping,
                "r2": float(fit.rsquared)},
    )


def _choose_d(y: np.ndarray, max_d: int = 2, alpha: float = 0.05) -> int:
    """Differencing order from an augmented Dickey-Fuller test.

    Keep differencing while the null of a unit root cannot be rejected, with a
    guard against over-differencing (which inflates variance).
    """
    from statsmodels.tsa.stattools import adfuller

    d, cur = 0, np.asarray(y, dtype=float)
    while d < max_d and len(cur) > 12:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                pvalue = adfuller(cur, autolag="AIC")[1]
        except Exception:
            break
        if pvalue < alpha:          # stationary enough
            break
        nxt = np.diff(cur)
        if nxt.var() > cur.var():   # over-differencing - back off
            break
        cur, d = nxt, d + 1
    return d


def arima_forecast(y: np.ndarray, horizon: int, max_p: int = 3, max_q: int = 3,
                   max_d: int = 2, damping: float = 1.0) -> ForecastResult:
    """Non-seasonal ARIMA(p, d, q); order chosen by AICc over a small grid."""
    from statsmodels.tsa.arima.model import ARIMA

    y = np.asarray(y, dtype=float)
    d = _choose_d(y, max_d=max_d)
    # 'c' is a mean for a stationary series; 't' is a linear trend, which after
    # differencing is the drift term that dominates a long-horizon forecast.
    trend = "c" if d == 0 else "t"

    best = best_order = None
    best_aicc = np.inf
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")   # convergence / stationarity chatter
        for p in range(max_p + 1):
            for q in range(max_q + 1):
                if p == 0 and q == 0 and d == 0:
                    continue            # a bare mean is not a useful model here
                try:
                    res = ARIMA(y, order=(p, d, q), trend=trend).fit()
                except Exception:
                    continue
                aicc = getattr(res, "aicc", np.inf)
                if np.isfinite(aicc) and aicc < best_aicc:
                    best, best_order, best_aicc = res, (p, d, q), aicc

    if best is None:    # grid came up empty - fall back rather than fail the run
        res = linear_forecast(y, horizon, damping=damping)
        res.params["fallback_from"] = "arima"
        return res

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        fc = best.get_forecast(steps=horizon)
        yhat = np.asarray(fc.predicted_mean, dtype=float)
        ci = np.asarray(fc.conf_int(alpha=0.05), dtype=float)
    lo, hi = ci[:, 0], ci[:, 1]
    yhat, lo, hi = _damp(float(y[-1]), yhat, lo, hi, damping)

    p, d, q = best_order
    return ForecastResult(
        model=f"arima({p},{d},{q})",
        yhat=yhat.tolist(), lo=lo.tolist(), hi=hi.tolist(),
        params={"p": p, "d": d, "q": q, "aicc": float(best_aicc),
                "aic": float(best.aic), "bic": float(best.bic),
                "sigma2": float(best.params[-1]), "damping": damping, "n": len(y)},
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
    """Extrapolate a daily series out to `until` (inclusive, ISO date strings).

    `log_space` fits ARIMA on log(1 + y) and exponentiates back: the trend
    becomes multiplicative and a negative forecast becomes structurally
    impossible. Deliberately not applied to the linear model - "simple linear
    regression" should draw a straight line, and compounding a slope estimated
    from two weeks of data over two years produces nonsense.
    """
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
    log_space = log_space and use_arima          # straight lines stay straight

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
        "params": dict(res.params),
        "points": points,
    }
