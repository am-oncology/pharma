"""Price-derived factors.

Everything here is a pure function over a price frame so it can be tested
without touching the network. The frame must have columns:
    open, high, low, close, volume
indexed by ascending date.

A caution that shaped these choices: small and mid-cap biotech prices are not
diffusion processes. They are step functions punctuated by binary readouts.
Momentum and trend factors carry real information about *sector rotation and
fund flows*, which is what actually drives the 80% of days with no company
news. They carry essentially none about the readout itself. The gap_events
factor exists to tell you which names are in the second regime, so the
momentum reading can be discounted accordingly.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

TRADING_DAYS = 252


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------
def pct_return(close: pd.Series, days: int) -> float:
    """Simple return over `days` trading days. NaN if insufficient history."""
    if len(close) < days + 1:
        return float("nan")
    start, end = close.iloc[-days - 1], close.iloc[-1]
    if not np.isfinite(start) or start <= 0:
        return float("nan")
    return float(end / start - 1.0)


def sma(close: pd.Series, days: int) -> float:
    if len(close) < days:
        return float("nan")
    return float(close.iloc[-days:].mean())


def sma_gap(close: pd.Series, days: int) -> float:
    """Fractional distance of last close above its moving average."""
    m = sma(close, days)
    if not np.isfinite(m) or m <= 0:
        return float("nan")
    return float(close.iloc[-1] / m - 1.0)


def rsi(close: pd.Series, period: int = 14) -> float:
    """Wilder's RSI. Returns 50 (neutral) when undefined."""
    if len(close) < period + 1:
        return float("nan")
    delta = close.diff().dropna()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean().iloc[-1]
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean().iloc[-1]
    if avg_loss <= 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return float(100.0 - 100.0 / (1.0 + rs))


def realised_vol(close: pd.Series, days: int = 63) -> float:
    """Annualised realised volatility from log returns."""
    if len(close) < days + 1:
        return float("nan")
    logret = np.log(close / close.shift(1)).dropna().iloc[-days:]
    if len(logret) < 5:
        return float("nan")
    return float(logret.std(ddof=1) * math.sqrt(TRADING_DAYS))


def atr_pct(df: pd.DataFrame, period: int = 14) -> float:
    """Average true range as a fraction of price."""
    if len(df) < period + 1:
        return float("nan")
    high, low, close = df["high"], df["low"], df["close"]
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean().iloc[-1]
    last = close.iloc[-1]
    if not np.isfinite(last) or last <= 0:
        return float("nan")
    return float(atr / last)


def volume_zscore(volume: pd.Series, short: int = 5, long: int = 63) -> float:
    """How unusual is recent volume against its own recent distribution.

    Elevated volume ahead of a known catalyst date is one of the few
    price-derived signals in this sector with a defensible mechanism.
    """
    if len(volume) < long + short:
        return float("nan")
    window = volume.iloc[-long:]
    mu, sd = window.mean(), window.std(ddof=1)
    if not np.isfinite(sd) or sd <= 0:
        return float("nan")
    recent = volume.iloc[-short:].mean()
    return float((recent - mu) / sd)


def accumulation(df: pd.DataFrame, days: int = 21) -> float:
    """Share of recent volume transacted on up days, centred at zero.

    +0.5 means all recent volume came on advancing days. A crude proxy for
    whether volume expansion is buying or selling.
    """
    if len(df) < days + 1:
        return float("nan")
    recent = df.iloc[-days:]
    direction = np.sign(recent["close"].diff().fillna(0.0))
    vol = recent["volume"]
    total = vol.sum()
    if total <= 0:
        return float("nan")
    return float((vol * direction).sum() / total / 2.0)


def drawdown_from_high(close: pd.Series, days: int = TRADING_DAYS) -> float:
    """Fractional distance below the rolling high. Always <= 0."""
    if len(close) < 20:
        return float("nan")
    window = close.iloc[-days:]
    peak = window.max()
    if not np.isfinite(peak) or peak <= 0:
        return float("nan")
    return float(close.iloc[-1] / peak - 1.0)


def gap_events(close: pd.Series, threshold: float = 0.15, days: int = TRADING_DAYS) -> int:
    """Count of single-day moves beyond `threshold` in the lookback.

    High counts mark binary-event names where continuous-time momentum
    statistics should not be read at face value.
    """
    if len(close) < 20:
        return 0
    ret = close.pct_change().dropna().iloc[-days:]
    return int((ret.abs() > threshold).sum())


def dollar_volume(df: pd.DataFrame, days: int = 21) -> float:
    if len(df) < 5:
        return 0.0
    recent = df.iloc[-days:]
    return float((recent["close"] * recent["volume"]).median())


def beta_and_alpha(close: pd.Series, bench_close: pd.Series, days: int = 126) -> tuple[float, float]:
    """OLS beta against the benchmark, and annualised residual alpha.

    Separating company-specific drift from sector beta is the single most
    useful thing you can do to a biotech price series. A name up 20% while
    XBI is up 22% has not done anything.
    """
    joined = pd.concat(
        [close.pct_change(), bench_close.pct_change()], axis=1, join="inner"
    ).dropna()
    joined.columns = ["stock", "bench"]
    joined = joined.iloc[-days:]
    if len(joined) < 30:
        return float("nan"), float("nan")
    var = joined["bench"].var(ddof=1)
    if not np.isfinite(var) or var <= 0:
        return float("nan"), float("nan")
    beta = float(joined["stock"].cov(joined["bench"]) / var)
    resid = joined["stock"] - beta * joined["bench"]
    alpha = float(resid.mean() * TRADING_DAYS)
    return beta, alpha


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
def compute_factors(df: pd.DataFrame, bench: pd.DataFrame | None) -> dict[str, Any]:
    """Compute the full price factor set for one instrument."""
    close, volume = df["close"], df["volume"]
    bench_close = bench["close"] if bench is not None and len(bench) else None

    f: dict[str, Any] = {
        "price": float(close.iloc[-1]),
        "history_days": int(len(df)),
        "ret_1d": pct_return(close, 1),
        "ret_5d": pct_return(close, 5),
        "ret_21d": pct_return(close, 21),
        "ret_63d": pct_return(close, 63),
        "ret_126d": pct_return(close, 126),
        "ret_252d": pct_return(close, 252),
        "sma50_gap": sma_gap(close, 50),
        "sma200_gap": sma_gap(close, 200),
        "rsi14": rsi(close, 14),
        "vol_ann": realised_vol(close, 63),
        "vol_ann_252": realised_vol(close, 252),
        "atr_pct": atr_pct(df, 14),
        "volume_z": volume_zscore(volume),
        "accumulation": accumulation(df, 21),
        "drawdown_52w": drawdown_from_high(close, TRADING_DAYS),
        "gap_events": gap_events(close),
        "dollar_volume": dollar_volume(df),
    }

    # Relative strength against the sector benchmark.
    for horizon in (21, 63, 126):
        key = f"rs_{horizon}d"
        if bench_close is not None:
            b = pct_return(bench_close, horizon)
            s = f[f"ret_{horizon}d"]
            f[key] = float(s - b) if np.isfinite(s) and np.isfinite(b) else float("nan")
        else:
            f[key] = float("nan")

    if bench_close is not None:
        f["beta"], f["alpha_ann"] = beta_and_alpha(close, bench_close)
    else:
        f["beta"], f["alpha_ann"] = float("nan"), float("nan")

    # Vol-of-vol: short-horizon volatility running hot versus its own annual
    # baseline often precedes a dated event.
    if np.isfinite(f["vol_ann"]) and np.isfinite(f["vol_ann_252"]) and f["vol_ann_252"] > 0:
        f["vol_expansion"] = float(f["vol_ann"] / f["vol_ann_252"] - 1.0)
    else:
        f["vol_expansion"] = float("nan")

    # Trend: a bounded composite of the two moving-average gaps.
    gaps = [f["sma50_gap"], f["sma200_gap"]]
    valid = [g for g in gaps if np.isfinite(g)]
    f["trend"] = float(np.tanh(np.mean(valid) * 4.0)) if valid else float("nan")

    # Distance below the 200d line, but only counted when the trend is broken.
    s200 = f["sma200_gap"]
    f["broken_trend"] = float(-s200) if np.isfinite(s200) and s200 < 0 else 0.0

    # Symmetric penalty for stretched RSI in either direction.
    r = f["rsi14"]
    f["reversion_stretch"] = float(abs(r - 50.0) / 50.0) if np.isfinite(r) else 0.0

    f["liquid"] = bool(
        f["dollar_volume"] >= 0 and np.isfinite(f["price"])
    )
    return f


# ---------------------------------------------------------------------------
# Cross-sectional standardisation
# ---------------------------------------------------------------------------
def winsorise(values: np.ndarray, limit: float = 0.05) -> np.ndarray:
    """Clip to the [limit, 1-limit] quantiles to stop one 400% mover from
    flattening the entire cross-section."""
    finite = values[np.isfinite(values)]
    if len(finite) < 5:
        return values
    lo, hi = np.quantile(finite, [limit, 1 - limit])
    return np.clip(values, lo, hi)


def zscore(values: np.ndarray, winsor: float = 0.05) -> np.ndarray:
    """Cross-sectional z-score. Non-finite entries map to 0 (neutral)."""
    vals = winsorise(np.asarray(values, dtype=float), winsor)
    finite = vals[np.isfinite(vals)]
    if len(finite) < 3:
        return np.zeros_like(vals)
    mu = finite.mean()
    sd = finite.std(ddof=1)
    if not np.isfinite(sd) or sd <= 0:
        return np.zeros_like(vals)
    out = (vals - mu) / sd
    return np.nan_to_num(out, nan=0.0, posinf=3.0, neginf=-3.0)
