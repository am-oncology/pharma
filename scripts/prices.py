"""Daily OHLCV ingest.

Uses yfinance, which is unofficial and occasionally changes shape underneath
you. Everything is defensive as a result.

A note on survivorship bias, because it silently ruins backtests in this
sector more than any other: yfinance serves the *current* constituent list.
Companies that were acquired, delisted or went bankrupt are simply absent.
Biotech has an unusually high rate of all three, so any historical study built
on a universe assembled today will look far better than reality. This module
records a per-ticker health log so at least you can see the universe drifting.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import pandas as pd

from config import LOOKBACK_DAYS, MIN_HISTORY_DAYS, MIN_PRICE, MIN_DOLLAR_VOLUME
from util import iso, utcnow

log = logging.getLogger("radar.prices")


def _normalise_frame(raw: pd.DataFrame, ticker: str) -> pd.DataFrame | None:
    """Coerce whatever yfinance returned into lowercase OHLCV."""
    if raw is None or len(raw) == 0:
        return None
    df = raw.copy()

    if isinstance(df.columns, pd.MultiIndex):
        # Batch downloads return (field, ticker) or (ticker, field).
        levels = [list(df.columns.get_level_values(i)) for i in range(df.columns.nlevels)]
        if ticker in levels[0]:
            df = df.xs(ticker, axis=1, level=0)
        elif ticker in levels[-1]:
            df = df.xs(ticker, axis=1, level=-1)
        else:
            df.columns = df.columns.get_level_values(0)

    df.columns = [str(c).lower().replace(" ", "_") for c in df.columns]
    if "adj_close" in df.columns and "close" in df.columns:
        # Prefer split/dividend-adjusted series for return calculations.
        ratio = (df["adj_close"] / df["close"]).replace([float("inf")], 1.0).fillna(1.0)
        for col in ("open", "high", "low"):
            if col in df.columns:
                df[col] = df[col] * ratio
        df["close"] = df["adj_close"]

    needed = ["open", "high", "low", "close", "volume"]
    for col in needed:
        if col not in df.columns:
            if col == "volume":
                df["volume"] = 0.0
            elif "close" in df.columns:
                df[col] = df["close"]
            else:
                return None

    df = df[needed].apply(pd.to_numeric, errors="coerce")
    df = df.dropna(subset=["close"])
    df = df[df["close"] > 0]
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, errors="coerce")
    df = df[~df.index.isna()].sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df if len(df) else None


def download(tickers: list[str]) -> dict[str, pd.DataFrame]:
    """Fetch daily bars for every ticker. Batch first, retry stragglers singly."""
    try:
        import yfinance as yf
    except ImportError:
        log.error("yfinance not installed; run pip install -r requirements.txt")
        return {}

    out: dict[str, pd.DataFrame] = {}
    period = f"{max(LOOKBACK_DAYS, 400)}d"

    try:
        batch = yf.download(
            tickers=" ".join(tickers), period=period, interval="1d",
            group_by="ticker", auto_adjust=False, actions=False,
            progress=False, threads=True,
        )
    except Exception as exc:  # noqa: BLE001 - yfinance raises freely
        log.warning("batch download failed, falling back to per-ticker: %s", exc)
        batch = None

    for ticker in tickers:
        frame = None
        if batch is not None and len(batch):
            try:
                frame = _normalise_frame(batch, ticker)
            except Exception:  # noqa: BLE001
                frame = None
        if frame is None or len(frame) < 30:
            # Only reached when the batch missed this name. If the batch
            # failed wholesale this runs for every ticker, so it is throttled:
            # 70 rapid crumb-authenticated requests is exactly what trips
            # Yahoo's rate limiter.
            time.sleep(0.6)
            try:
                single = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=False)
                frame = _normalise_frame(single, ticker)
            except Exception as exc:  # noqa: BLE001
                log.warning("%s: no price data (%s)", ticker, exc)
                frame = None
        if frame is not None:
            out[ticker] = frame
    log.info("prices: %d/%d tickers returned data", len(out), len(tickers))
    if tickers and len(out) < len(tickers) * 0.8:
        log.error(
            "prices: only %d/%d tickers returned data. Factors are computed "
            "cross-sectionally, so a thin universe distorts every z-score and "
            "therefore every bucket. Treat this board as unreliable.",
            len(out), len(tickers),
        )
    return out


def fetch_profiles(tickers: list[str], pause: float = 1.2) -> dict[str, dict[str, Any]]:
    """Market cap, short interest, float. Best-effort; often partially empty.

    This is the most rate-limit-prone call in the pipeline. `.info` is a
    separate crumb-authenticated request per ticker, and GitHub runners share
    IP space with a great deal of scraping, so Yahoo starts returning 429 well
    before 70 tickers are done. Requests are therefore throttled, and a run of
    consecutive failures aborts the whole step rather than spending twenty
    minutes collecting nothing.

    Nothing here feeds the composite score — profiles are display metadata
    only — so degrading to empty is survivable. It is not survivable silently,
    hence the coverage warning.
    """
    try:
        import yfinance as yf
    except ImportError:
        return {}

    profiles: dict[str, dict[str, Any]] = {}
    consecutive_failures = 0
    for i, ticker in enumerate(tickers):
        if i:
            time.sleep(pause)
        try:
            info = yf.Ticker(ticker).info or {}
            consecutive_failures = 0
        except Exception as exc:  # noqa: BLE001
            consecutive_failures += 1
            log.warning("%s: profile fetch failed (%s)", ticker, exc)
            if consecutive_failures >= 8:
                log.error(
                    "profiles: %d consecutive failures, almost certainly rate "
                    "limiting. Abandoning after %d/%d; the board still builds, "
                    "market caps and analyst targets will be blank.",
                    consecutive_failures, len(profiles), len(tickers),
                )
                break
            continue
        profiles[ticker] = {
            "market_cap": info.get("marketCap"),
            "shares_out": info.get("sharesOutstanding"),
            "float_shares": info.get("floatShares"),
            "short_pct_float": info.get("shortPercentOfFloat"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "employees": info.get("fullTimeEmployees"),
            "analyst_target": info.get("targetMeanPrice"),
            "recommendation": info.get("recommendationKey"),
            "next_earnings": info.get("earningsTimestamp"),
        }
    if tickers and len(profiles) < len(tickers) * 0.5:
        log.warning("profiles: only %d/%d returned — Yahoo is throttling",
                    len(profiles), len(tickers))
    return profiles


def eligibility(frame: pd.DataFrame) -> tuple[bool, str]:
    """Is this name liquid and long-lived enough to rank?

    Ranking an illiquid microcap produces a number you cannot act on: the
    spread eats the edge and you cannot exit into bad news.
    """
    if len(frame) < MIN_HISTORY_DAYS:
        return False, f"only {len(frame)}d of history"
    last = float(frame["close"].iloc[-1])
    if last < MIN_PRICE:
        return False, f"price ${last:.2f} below floor"
    recent = frame.iloc[-21:]
    dv = float((recent["close"] * recent["volume"]).median())
    if dv < MIN_DOLLAR_VOLUME:
        return False, f"median turnover ${dv/1e6:.2f}M below floor"
    return True, ""


def movers(factors: dict[str, dict], names: dict[str, str], top: int = 12) -> dict[str, list]:
    """Session and week movers, for the top-of-page strip."""
    rows = []
    for ticker, f in factors.items():
        d1, d5 = f.get("ret_1d"), f.get("ret_5d")
        if d1 is None or not isinstance(d1, (int, float)) or d1 != d1:
            continue
        rows.append({
            "ticker": ticker,
            "name": names.get(ticker, ticker),
            "price": f.get("price"),
            "ret_1d": d1,
            "ret_5d": d5 if isinstance(d5, (int, float)) and d5 == d5 else None,
            "volume_z": f.get("volume_z"),
        })
    by_day = sorted(rows, key=lambda r: r["ret_1d"], reverse=True)
    by_week = sorted(
        [r for r in rows if r["ret_5d"] is not None],
        key=lambda r: r["ret_5d"], reverse=True,
    )
    return {
        "gainers_1d": by_day[:top],
        "losers_1d": by_day[-top:][::-1],
        "gainers_5d": by_week[:top],
        "losers_5d": by_week[-top:][::-1],
        "unusual_volume": sorted(
            [r for r in rows if isinstance(r["volume_z"], (int, float)) and r["volume_z"] == r["volume_z"]],
            key=lambda r: r["volume_z"], reverse=True,
        )[:top],
        "as_of": iso(utcnow()),
    }
