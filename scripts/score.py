"""Composite scoring and bucketing.

Each factor is z-scored across the eligible universe, multiplied by its
weight, and summed. The per-factor contributions are kept and published
alongside the total so every ranking can be taken apart and argued with.

What the output is: a ranked triage list. "Constructive" means the factors
currently line up favourably and the name is worth your reading time.
"Deteriorating" means something is breaking and you should look at it before
the market finishes repricing it. Neither is a recommendation to transact.

What the output is not: an edge. Every input here is public, most of it is
machine-readable, and the sector is covered by specialist funds with medical
staff. If a mechanical rule over this data worked, it would already be
arbitraged out. The value is in surfacing the right dozen things each
morning so your own clinical judgement gets applied to them.
"""
from __future__ import annotations

import logging
import math
from typing import Any

import numpy as np

from config import BUCKETS, CATALYST_WINDOW_DAYS, WEIGHTS
from signals import zscore
from util import days_between, parse_date, utcnow

log = logging.getLogger("radar.score")

# Human-readable labels for the factor breakdown chart.
FACTOR_LABELS = {
    "relative_strength": "Relative strength vs XBI",
    "trend": "Trend (50d / 200d)",
    "long_momentum": "6-month momentum",
    "accumulation": "Accumulation",
    "news_sentiment": "News and events",
    "catalyst_proximity": "Catalyst proximity",
    "trial_momentum": "Pipeline momentum",
    "reversion_penalty": "Stretched (mean reversion)",
    "dilution_risk": "Financing risk",
    "drawdown_risk": "Broken trend",
}


def catalyst_proximity(catalysts: list[dict], earnings_days: float | None) -> float:
    """How much dated event risk sits in the forward window.

    Note this is unsigned: an imminent Phase III readout is enormous
    *variance*, not enormous expected return. It scores positively because
    dated events attract flows and optionality, but read it as "something is
    about to happen here", not "this will go up".
    """
    total = 0.0
    for c in catalysts:
        days = c.get("days_out")
        if days is None or days < -30 or days > CATALYST_WINDOW_DAYS:
            continue
        proximity = math.exp(-max(days, 0) / 60.0)
        total += c.get("phase_weight", 0.4) * proximity
    if earnings_days is not None and 0 <= earnings_days <= 45:
        total += 0.25 * math.exp(-earnings_days / 30.0)
    return float(2.5 * math.tanh(total / 1.5))


def _raw_factors(
    ticker: str,
    price: dict[str, Any],
    news_sentiment: float,
    trial_momentum: float,
    dilution: float,
    catalysts: list[dict],
    earnings_days: float | None,
) -> dict[str, float]:
    """Assemble the raw (pre-z-score) factor vector for one name."""

    def num(key: str, default: float = 0.0) -> float:
        v = price.get(key)
        return float(v) if isinstance(v, (int, float)) and np.isfinite(v) else default

    # Blend 1m and 3m relative strength, favouring the longer window.
    rs = 0.35 * num("rs_21d") + 0.65 * num("rs_63d")

    return {
        "relative_strength": rs,
        "trend": num("trend"),
        "long_momentum": num("ret_126d"),
        "accumulation": num("accumulation") * (1.0 + 0.3 * np.tanh(num("volume_z"))),
        "news_sentiment": news_sentiment,
        "catalyst_proximity": catalyst_proximity(catalysts, earnings_days),
        "trial_momentum": trial_momentum,
        "reversion_penalty": num("reversion_stretch"),
        "dilution_risk": dilution,
        "drawdown_risk": num("broken_trend"),
    }


def _describe(ticker: str, contributions: dict[str, float], context: dict[str, Any]) -> list[str]:
    """Plain-language reasons, strongest first.

    Each line names the factor, its direction and the underlying number, so
    the reason is checkable rather than assertive.
    """
    ordered = sorted(contributions.items(), key=lambda kv: abs(kv[1]), reverse=True)
    price = context["price"]
    lines: list[str] = []

    for factor, contribution in ordered[:5]:
        if abs(contribution) < 0.04:
            continue
        up = contribution > 0

        if factor == "relative_strength":
            rs = price.get("rs_63d")
            if isinstance(rs, (int, float)) and np.isfinite(rs):
                lines.append(
                    f"{'Outperforming' if rs > 0 else 'Lagging'} XBI by "
                    f"{abs(rs)*100:.0f} points over three months."
                )
        elif factor == "trend":
            g50, g200 = price.get("sma50_gap"), price.get("sma200_gap")
            if isinstance(g50, (int, float)) and np.isfinite(g50):
                above = "above" if g50 > 0 else "below"
                tail = ""
                if isinstance(g200, (int, float)) and np.isfinite(g200):
                    tail = f" and {abs(g200)*100:.0f}% {'above' if g200 > 0 else 'below'} its 200-day"
                lines.append(f"Trading {abs(g50)*100:.0f}% {above} its 50-day average{tail}.")
        elif factor == "long_momentum":
            r = price.get("ret_126d")
            if isinstance(r, (int, float)) and np.isfinite(r):
                lines.append(f"{'Up' if r > 0 else 'Down'} {abs(r)*100:.0f}% over six months.")
        elif factor == "accumulation":
            vz = price.get("volume_z")
            if isinstance(vz, (int, float)) and np.isfinite(vz) and abs(vz) > 0.5:
                lines.append(
                    f"Volume running {vz:+.1f} standard deviations from normal, "
                    f"{'on up days' if up else 'on down days'}."
                )
        elif factor == "news_sentiment":
            top = context.get("top_headline")
            if top:
                lines.append(f"{'Positive' if up else 'Negative'} coverage: {top}")
            else:
                lines.append(f"Net {'positive' if up else 'negative'} news flow this month.")
        elif factor == "catalyst_proximity":
            nxt = context.get("next_catalyst")
            if nxt:
                phase = "/".join(nxt.get("phases", [])) or "trial"
                lines.append(
                    f"{phase} readout window opens in {nxt['days_out']} days "
                    f"({nxt['nct_id']})."
                )
        elif factor == "trial_momentum":
            changes = context.get("trial_changes", [])
            if changes:
                c = changes[0]
                if c["type"] == "status":
                    lines.append(
                        f"Registry status moved {c['from'].replace('_',' ').lower()} → "
                        f"{c['to'].replace('_',' ').lower()} on {c['nct_id']}."
                    )
                else:
                    direction = "slipped" if c["shift_days"] > 0 else "pulled forward"
                    lines.append(
                        f"Primary completion {direction} {abs(c['shift_days'])} days on {c['nct_id']}."
                    )
            else:
                lines.append(f"Pipeline activity net {'positive' if up else 'negative'}.")
        elif factor == "reversion_penalty":
            r = price.get("rsi14")
            if isinstance(r, (int, float)) and np.isfinite(r):
                lines.append(f"RSI at {r:.0f} — stretched, so entry timing matters.")
        elif factor == "dilution_risk":
            reasons = context.get("dilution_reasons", [])
            if reasons:
                lines.append(reasons[0].capitalize() + ".")
        elif factor == "drawdown_risk":
            dd = price.get("drawdown_52w")
            if isinstance(dd, (int, float)) and np.isfinite(dd):
                lines.append(f"{abs(dd)*100:.0f}% below its 52-week high.")

    gaps = price.get("gap_events", 0)
    if isinstance(gaps, int) and gaps >= 3:
        lines.append(
            f"Binary-event profile: {gaps} single-day moves above 15% in the past year, "
            f"so momentum readings here are weak evidence."
        )
    return lines


def rank(
    universe: list[dict],
    price_factors: dict[str, dict],
    news: dict[str, Any],
    trial_data: dict[str, Any],
    edgar_data: dict[str, Any],
    profiles: dict[str, dict],
    excluded: set[str],
    holdings: set[str],
    eligible: dict[str, tuple[bool, str]],
) -> dict[str, Any]:
    """Score, rank and bucket the universe."""
    names = {c["ticker"]: c["name"] for c in universe}
    tags = {c["ticker"]: c.get("tags", []) for c in universe}
    tiers = {c["ticker"]: c.get("tier", "small") for c in universe}
    now = utcnow()

    catalysts_by_ticker: dict[str, list[dict]] = {}
    for c in trial_data.get("catalysts", []):
        catalysts_by_ticker.setdefault(c["ticker"], []).append(c)

    changes_by_ticker: dict[str, list[dict]] = {}
    for c in trial_data.get("changes", []):
        changes_by_ticker.setdefault(c["ticker"], []).append(c)

    ranked_tickers: list[str] = []
    skipped: list[dict] = []
    raw_rows: dict[str, dict[str, float]] = {}

    for company in universe:
        ticker = company["ticker"]
        if ticker in excluded:
            skipped.append({"ticker": ticker, "reason": "excluded for conflict of interest"})
            continue
        if ticker not in price_factors:
            skipped.append({"ticker": ticker, "reason": "no price data returned"})
            continue
        ok, why = eligible.get(ticker, (True, ""))
        if not ok:
            skipped.append({"ticker": ticker, "reason": why})
            continue

        earnings_days = None
        ts = (profiles.get(ticker) or {}).get("next_earnings")
        if isinstance(ts, (int, float)) and ts > 0:
            import datetime as _dt
            try:
                earnings_days = days_between(
                    _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc), now
                )
            except (ValueError, OSError):
                earnings_days = None

        raw_rows[ticker] = _raw_factors(
            ticker,
            price_factors[ticker],
            news.get("sentiment", {}).get(ticker, 0.0),
            trial_data.get("scores", {}).get(ticker, 0.0),
            (edgar_data.get("dilution", {}).get(ticker) or {}).get("score", 0.0),
            catalysts_by_ticker.get(ticker, []),
            earnings_days,
        )
        ranked_tickers.append(ticker)

    if not ranked_tickers:
        return {"rows": [], "skipped": skipped, "as_of": now.isoformat()}

    # Cross-sectional standardisation, one factor at a time.
    z: dict[str, np.ndarray] = {}
    for factor in WEIGHTS:
        values = np.array([raw_rows[t].get(factor, 0.0) for t in ranked_tickers], dtype=float)
        z[factor] = zscore(values)

    rows: list[dict] = []
    for i, ticker in enumerate(ranked_tickers):
        contributions = {f: float(z[f][i] * w) for f, w in WEIGHTS.items()}
        composite = float(sum(contributions.values()))

        articles = sorted(
            news.get("per_ticker", {}).get(ticker, []),
            key=lambda a: abs(a.get("decayed_score", 0.0)), reverse=True,
        )
        hard = news.get("hard_flags", {}).get(ticker, [])
        hard_trials = [c for c in changes_by_ticker.get(ticker, []) if c.get("hard")]

        context = {
            "price": price_factors[ticker],
            "next_catalyst": (catalysts_by_ticker.get(ticker) or [None])[0],
            "trial_changes": changes_by_ticker.get(ticker, []),
            "dilution_reasons": (edgar_data.get("dilution", {}).get(ticker) or {}).get("reasons", []),
            "top_headline": articles[0]["title"][:150] if articles else None,
        }
        reasons = _describe(ticker, contributions, context)

        # Hard flags override the composite. A momentum score cannot outvote
        # a clinical hold, and pretending otherwise is how these tools do
        # real damage.
        override = None
        if hard or hard_trials:
            override = "hard_flag"
            # Subtract a severity penalty rather than clamping to a fixed
            # floor: clamping made every flagged name score identically, so
            # the ordering within the list became arbitrary and a company
            # with three failures ranked alongside one with a single shelf
            # filing. The floor still guarantees the deteriorating bucket.
            severity = 0.7 * len(hard) + 0.9 * len(hard_trials)
            composite = min(composite - severity, -1.25)
            for flag in hard[:2]:
                reasons.insert(0, f"{flag['triggers'][0].title()} reported — {flag['title'][:110]}")
            for change in hard_trials[:1]:
                reasons.insert(
                    0, f"{change['nct_id']} moved to {change['to'].replace('_',' ').lower()}."
                )

        if composite >= BUCKETS["constructive"]:
            bucket = "constructive"
        elif composite <= BUCKETS["deteriorating"]:
            bucket = "deteriorating"
        else:
            bucket = "neutral"

        cash = edgar_data.get("cash", {}).get(ticker, {}) or {}
        profile = profiles.get(ticker, {}) or {}
        pf = price_factors[ticker]

        rows.append({
            "ticker": ticker,
            "name": names.get(ticker, ticker),
            "tier": tiers.get(ticker, "small"),
            "tags": tags.get(ticker, []),
            "held": ticker in holdings,
            "score": round(composite, 3),
            "bucket": bucket,
            "override": override,
            "contributions": {k: round(v, 4) for k, v in contributions.items()},
            "raw": {k: round(v, 4) for k, v in raw_rows[ticker].items()},
            "reasons": reasons[:5],
            "price": pf.get("price"),
            "ret_1d": pf.get("ret_1d"),
            "ret_5d": pf.get("ret_5d"),
            "ret_21d": pf.get("ret_21d"),
            "ret_63d": pf.get("ret_63d"),
            "ret_252d": pf.get("ret_252d"),
            "rs_63d": pf.get("rs_63d"),
            "rsi14": pf.get("rsi14"),
            "vol_ann": pf.get("vol_ann"),
            "beta": pf.get("beta"),
            "drawdown_52w": pf.get("drawdown_52w"),
            "gap_events": pf.get("gap_events"),
            "volume_z": pf.get("volume_z"),
            "market_cap": profile.get("market_cap"),
            "short_pct_float": profile.get("short_pct_float"),
            "runway_months": cash.get("runway_months"),
            "cash": cash.get("cash"),
            "catalysts": catalysts_by_ticker.get(ticker, [])[:4],
            "hard_flags": hard,
            "trial_changes": changes_by_ticker.get(ticker, [])[:4],
            "filings": [
                f for f in edgar_data.get("filings", {}).get(ticker, [])
                if f["dilutive"] or f["deal"] or f["days_ago"] <= 21
            ][:6],
            "headlines": [
                {
                    "title": a["title"][:180], "url": a.get("url", ""),
                    "source": a.get("source", ""), "published": a.get("published_iso", ""),
                    "score": a.get("raw_score", 0),
                }
                for a in articles[:6]
            ],
        })

    rows.sort(key=lambda r: r["score"], reverse=True)

    counts = {b: sum(1 for r in rows if r["bucket"] == b)
              for b in ("constructive", "neutral", "deteriorating")}
    log.info("ranked %d names: %s", len(rows), counts)

    return {
        "rows": rows,
        "skipped": skipped,
        "counts": counts,
        "weights": WEIGHTS,
        "labels": FACTOR_LABELS,
        "as_of": now.isoformat(),
    }


def build_calendar(trial_data: dict[str, Any], universe: list[dict],
                   conferences: list[dict], profiles: dict[str, dict]) -> list[dict]:
    """Merged forward calendar: readouts, conferences and earnings."""
    names = {c["ticker"]: c["name"] for c in universe}
    now = utcnow()
    events: list[dict] = []

    for c in trial_data.get("catalysts", []):
        if 0 <= c["days_out"] <= 365:
            events.append({
                "date": c["date"], "days_out": c["days_out"], "kind": "readout",
                "ticker": c["ticker"], "name": names.get(c["ticker"], c["ticker"]),
                "label": "/".join(c["phases"]) or "Trial",
                "detail": c["title"][:130], "nct_id": c["nct_id"],
                "estimated": c.get("estimated", True),
                "weight": c.get("phase_weight", 0.4),
            })

    for conf in conferences:
        start = parse_date(conf["start"])
        if not start:
            continue
        days_out = days_between(start, now)
        if -7 <= days_out <= 365:
            events.append({
                "date": conf["start"], "days_out": round(days_out), "kind": "conference",
                "ticker": "", "name": conf["name"], "label": "Conference",
                "detail": f"{conf['name']} · {', '.join(conf.get('tags', []))}",
                "nct_id": "", "estimated": False, "weight": 0.5,
            })

    import datetime as _dt
    for ticker, profile in profiles.items():
        ts = profile.get("next_earnings")
        if not isinstance(ts, (int, float)) or ts <= 0:
            continue
        try:
            when = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc)
        except (ValueError, OSError):
            continue
        days_out = days_between(when, now)
        if 0 <= days_out <= 120:
            events.append({
                "date": when.strftime("%Y-%m-%d"), "days_out": round(days_out),
                "kind": "earnings", "ticker": ticker,
                "name": names.get(ticker, ticker), "label": "Earnings",
                "detail": f"{names.get(ticker, ticker)} quarterly results",
                "nct_id": "", "estimated": True, "weight": 0.3,
            })

    events.sort(key=lambda e: e["days_out"])
    return events
