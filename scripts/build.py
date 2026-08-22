"""Pipeline entry point.

    python scripts/build.py             # full run, hits every live source
    python scripts/build.py --demo      # synthetic data, no network
    python scripts/build.py --no-edgar  # skip SEC (slowest stage)
    python scripts/build.py --quick     # prices and news only

Writes JSON into data/ for the static frontend to read.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd

import edgar as edgar_mod
import news as news_mod
import prices as prices_mod
import score as score_mod
import trials as trials_mod
from config import CONFERENCES, DATA, ROOT, RSS_FEEDS, WEIGHTS
from signals import compute_factors
from util import iso, setup_logging, utcnow, write_json

log = logging.getLogger("radar.build")


def load_universe() -> dict:
    path = ROOT / "universe.json"
    payload = json.loads(path.read_text())
    payload["companies"] = [c for c in payload["companies"] if c.get("ticker")]
    return payload


# ---------------------------------------------------------------------------
# Demo data
# ---------------------------------------------------------------------------
def _synthetic_prices(tickers: list[str], days: int = 500, seed: int = 7) -> dict[str, pd.DataFrame]:
    """Jump-diffusion series: continuous drift plus occasional binary events.

    Biotech prices are not smooth, and a demo built on smooth data would give
    a misleading impression of how the factors behave.
    """
    rng = np.random.default_rng(seed)
    # bdate_range(end=...) drops the endpoint when it is not itself a business
    # day, so anchoring to "today" silently yields periods-1 rows on weekends
    # and holidays. The Saturday cron used to crash here. Build the index
    # first and take the row count from it rather than assuming.
    index = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=days)
    n = len(index)
    out: dict[str, pd.DataFrame] = {}
    for ticker in tickers:
        h = abs(hash(ticker)) % 10_000
        local = np.random.default_rng(seed + h)
        drift = local.normal(0.0004, 0.0007)
        vol = local.uniform(0.018, 0.055)
        ret = local.normal(drift, vol, n)
        # Binary events: rare, large, and not predictable from the path.
        n_jumps = local.poisson(2.2)
        for _ in range(n_jumps):
            at = local.integers(0, n)
            ret[at] += local.choice([-1, 1], p=[0.45, 0.55]) * local.uniform(0.14, 0.55)
        price = 20 * np.exp(np.cumsum(ret))
        close = pd.Series(price, index=index)
        noise = local.uniform(0.004, 0.02, n)
        frame = pd.DataFrame({
            "open": close.shift(1).fillna(close.iloc[0]) * (1 + local.normal(0, 0.004, n)),
            "high": close * (1 + noise),
            "low": close * (1 - noise),
            "close": close,
            "volume": local.lognormal(13.5, 0.6, n) * (1 + 4 * np.abs(ret)),
        }, index=index)
        out[ticker] = frame
    return out


def _synthetic_context(companies: list[dict], seed: int = 7) -> tuple[dict, dict, dict, dict]:
    """Plausible news, trials, filings and profiles for offline testing."""
    rng = np.random.default_rng(seed)
    tickers = [c["ticker"] for c in companies]
    now = utcnow()

    templates_pos = [
        "{name} reports positive topline results from pivotal study",
        "FDA approves {name}'s lead candidate for advanced disease",
        "{name} granted breakthrough therapy designation",
        "{name} announces strategic collaboration with upfront payment",
    ]
    templates_neg = [
        "{name} study did not meet the primary endpoint",
        "{name} receives complete response letter from FDA",
        "{name} announces pricing of public offering",
        "{name} places trial on clinical hold after safety signal",
    ]

    articles, sentiment, hard_flags, per_ticker = [], {}, {}, {}
    for company in companies:
        local = np.random.default_rng(seed + (abs(hash(company["ticker"])) % 9999))
        for _ in range(int(local.integers(0, 4))):
            positive = local.random() > 0.42
            pool = templates_pos if positive else templates_neg
            title = str(local.choice(pool)).format(name=company["name"])
            age = float(local.uniform(0, 28))
            raw, hits = news_mod.score_text(title)
            decayed = raw * 0.5 ** (age / 7.0)
            article = {
                "source": str(local.choice(["FierceBiotech", "Endpoints News", "STAT News", "BioSpace"])),
                "title": title, "summary": "", "url": "https://example.invalid/demo",
                "published_iso": (now - pd.Timedelta(days=age)).strftime("%Y-%m-%d"),
                "age_days": round(age, 1), "raw_score": round(raw, 2),
                "decayed_score": round(decayed, 3), "hits": hits,
                "hard": any(h["hard"] for h in hits),
                "tickers": [company["ticker"]],
            }
            articles.append(article)
            per_ticker.setdefault(company["ticker"], []).append(article)
            sentiment[company["ticker"]] = sentiment.get(company["ticker"], 0.0) + decayed
            if article["hard"] and age <= 21:
                neg = [h for h in hits if h["hard"] and h["weight"] < 0]
                if neg:
                    hard_flags.setdefault(company["ticker"], []).append({
                        "title": title, "url": article["url"], "source": article["source"],
                        "published": article["published_iso"],
                        "triggers": [h["phrase"] for h in neg], "kind": neg[0]["kind"],
                    })
    for t, v in list(sentiment.items()):
        sentiment[t] = float(3.0 * np.tanh(v / 4.0))
    articles.sort(key=lambda a: a["published_iso"], reverse=True)

    catalysts, trial_scores, changes = [], {}, []
    conditions = ["Glioblastoma", "NSCLC", "Metastatic breast cancer", "Multiple myeloma",
                  "Colorectal cancer", "Pancreatic adenocarcinoma", "AML"]
    for company in companies:
        local = np.random.default_rng(seed + 31 + (abs(hash(company["ticker"])) % 9999))
        for _ in range(int(local.integers(0, 4))):
            days_out = int(local.integers(5, 330))
            phase = str(local.choice(["Phase 1", "Phase 2", "Phase 3"], p=[0.3, 0.4, 0.3]))
            catalysts.append({
                "ticker": company["ticker"],
                "nct_id": f"NCT{local.integers(10_000_000, 99_999_999)}",
                "title": f"A study of a candidate in {local.choice(conditions)}",
                "phases": [phase],
                "phase_weight": {"Phase 1": 0.25, "Phase 2": 0.6, "Phase 3": 1.0}[phase],
                "status": "ACTIVE_NOT_RECRUITING",
                "enrollment": int(local.integers(40, 900)),
                "conditions": [str(local.choice(conditions))],
                "date": (now + pd.Timedelta(days=days_out)).strftime("%Y-%m-%d"),
                "days_out": days_out, "estimated": True, "kind": "readout",
            })
        trial_scores[company["ticker"]] = float(local.normal(0.3, 0.8))
    catalysts.sort(key=lambda c: c["days_out"])

    filings, cash, dilution = {}, {}, {}
    for company in companies:
        local = np.random.default_rng(seed + 71 + (abs(hash(company["ticker"])) % 9999))
        runway = float(local.uniform(4, 60)) if company.get("tier") != "large" else 999.0
        cash[company["ticker"]] = {
            "cash": float(local.uniform(1e8, 5e9)), "cash_date": "2026-06-30",
            "quarterly_burn": -float(local.uniform(2e7, 3e8)),
            "runway_months": round(runway, 1), "quarters_used": 4,
        }
        fl = []
        if local.random() > 0.75:
            fl.append({
                "form": "424B5", "label": "priced offering",
                "filed": (now - pd.Timedelta(days=int(local.integers(3, 80)))).strftime("%Y-%m-%d"),
                "days_ago": int(local.integers(3, 80)), "description": "Prospectus supplement",
                "weight": -1.5, "dilutive": True, "deal": False,
                "url": "https://www.sec.gov/edgar",
            })
        filings[company["ticker"]] = fl
        s, reasons = edgar_mod.dilution_risk(cash[company["ticker"]], fl)
        dilution[company["ticker"]] = {"score": s, "reasons": reasons}

    profiles = {}
    for company in companies:
        local = np.random.default_rng(seed + 97 + (abs(hash(company["ticker"])) % 9999))
        scale = {"large": 2e11, "mid": 2e10, "small": 3e9}[company.get("tier", "small")]
        profiles[company["ticker"]] = {
            "market_cap": float(local.uniform(0.3, 2.0) * scale),
            "short_pct_float": float(local.uniform(0.01, 0.18)),
            "next_earnings": (now + pd.Timedelta(days=int(local.integers(3, 95)))).timestamp(),
            "sector": "Healthcare",
        }

    news_out = {"articles": articles, "sentiment": sentiment,
                "hard_flags": hard_flags, "per_ticker": per_ticker}
    trials_out = {"by_ticker": {}, "changes": changes,
                  "catalysts": catalysts, "scores": trial_scores}
    edgar_out = {"filings": filings, "cash": cash, "dilution": dilution, "cik": {}}
    return news_out, trials_out, edgar_out, profiles


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
def _preflight() -> int:
    """Confirm every live source answers before you trust a board built from it.

    Worth running once on any new machine or runner. A source that is merely
    unreachable degrades quietly — the pipeline still emits a full-looking
    board with that factor silently zeroed — so the failure mode this catches
    is a page that looks complete and is not.
    """
    import requests

    from config import CTGOV_API, OPENFDA_DRUGSFDA, SEC_TICKER_MAP, USER_AGENT

    checks = [
        ("ClinicalTrials.gov", f"{CTGOV_API}?pageSize=1", True),
        ("SEC company tickers", SEC_TICKER_MAP, True),
        ("openFDA", f"{OPENFDA_DRUGSFDA}?limit=1", False),
        ("Yahoo Finance (prices)",
         "https://query1.finance.yahoo.com/v8/finance/chart/AZN?range=5d&interval=1d", True),
    ]
    checks += [(f"RSS · {name}", url, False) for name, url in RSS_FEEDS[:6]]

    failures, degraded = 0, 0
    for name, url, essential in checks:
        try:
            resp = requests.get(url, timeout=20, headers={"User-Agent": USER_AGENT})
            ok = resp.status_code == 200
            status = f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            ok, status = False, type(exc).__name__
        if ok:
            mark = "ok  "
        elif essential:
            mark, failures = "FAIL", failures + 1
        else:
            mark, degraded = "warn", degraded + 1
        log.info("%-4s %-28s %s", mark, name, status)

    if os.environ.get("RADAR_CONTACT"):
        log.info("ok   RADAR_CONTACT set")
    else:
        log.warning("warn RADAR_CONTACT unset — the SEC throttles anonymous callers")
        degraded += 1

    if failures:
        log.error("%d essential source(s) unreachable; a live build would be "
                  "incomplete rather than empty", failures)
        return 1
    log.info("preflight passed%s", f" with {degraded} degraded" if degraded else "")
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Build the Biotech Radar dataset")
    ap.add_argument("--demo", action="store_true", help="synthetic data, no network")
    ap.add_argument("--preflight", action="store_true",
                    help="check every live source is reachable, then exit")
    ap.add_argument("--no-edgar", action="store_true", help="skip SEC ingest")
    ap.add_argument("--no-trials", action="store_true", help="skip ClinicalTrials.gov")
    ap.add_argument("--quick", action="store_true", help="prices and news only")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    setup_logging(args.verbose)
    started = time.time()

    if args.preflight:
        return _preflight()

    universe = load_universe()
    companies = universe["companies"]
    benchmarks = universe["benchmarks"]
    excluded = set(universe.get("excluded", {}).get("tickers", []))
    holdings = set(universe.get("holdings", {}).get("tickers", []))
    tickers = [c["ticker"] for c in companies]
    bench_tickers = list(dict.fromkeys(benchmarks.values()))

    log.info("universe: %d companies, %d excluded, %d held",
             len(companies), len(excluded), len(holdings))

    # --- prices -----------------------------------------------------------
    if args.demo:
        log.info("demo mode: generating synthetic price series")
        frames = _synthetic_prices(tickers + bench_tickers)
        profiles_live = {}
    else:
        frames = prices_mod.download(tickers + bench_tickers)
        profiles_live = prices_mod.fetch_profiles(tickers)

    bench_frame = frames.get(benchmarks["sector"])
    if bench_frame is None or len(bench_frame) == 0:
        bench_frame = frames.get(benchmarks.get("sector_alt", ""))
    if bench_frame is None or len(bench_frame) == 0:
        bench_frame = None
        log.warning("no sector benchmark data; relative strength will be unavailable")

    price_factors, eligible = {}, {}
    for ticker in tickers:
        frame = frames.get(ticker)
        if frame is None or len(frame) < 30:
            continue
        try:
            price_factors[ticker] = compute_factors(frame, bench_frame)
            eligible[ticker] = prices_mod.eligibility(frame)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: factor computation failed: %s", ticker, exc)

    bench_factors = {}
    for b in bench_tickers:
        if b in frames:
            bench_factors[b] = compute_factors(frames[b], bench_frame)

    log.info("computed factors for %d names", len(price_factors))

    # --- context ----------------------------------------------------------
    if args.demo:
        news_data, trial_data, edgar_data, profiles = _synthetic_context(companies)
        trial_data.setdefault("matched_companies", len(trial_data.get("by_ticker", {})))
        trial_data.setdefault("rejected_studies", 0)
    else:
        profiles = profiles_live
        news_data = news_mod.build(companies)
        trial_data = ({"by_ticker": {}, "changes": [], "catalysts": [], "scores": {}}
                      if args.no_trials or args.quick else trials_mod.ingest(companies))
        edgar_data = ({"filings": {}, "cash": {}, "dilution": {}, "cik": {}}
                      if args.no_edgar or args.quick else edgar_mod.ingest(companies))

    # --- rank -------------------------------------------------------------
    ranking = score_mod.rank(
        companies, price_factors, news_data, trial_data, edgar_data,
        profiles, excluded, holdings, eligible,
    )
    calendar = score_mod.build_calendar(trial_data, companies, CONFERENCES, profiles)
    mover_data = prices_mod.movers(price_factors, {c["ticker"]: c["name"] for c in companies})

    rows = ranking["rows"]
    constructive = [r for r in rows if r["bucket"] == "constructive"]
    # Worst first — the point of this list is triage, and the most broken
    # name is the one you need to look at before the market finishes repricing.
    deteriorating = sorted(
        [r for r in rows if r["bucket"] == "deteriorating"], key=lambda r: r["score"]
    )

    now = utcnow()
    write_json("radar.json", {
        "as_of": iso(now),
        "demo": args.demo,
        "universe_size": len(companies),
        "ranked": len(rows),
        "counts": ranking.get("counts", {}),
        "weights": WEIGHTS,
        "labels": ranking.get("labels", {}),
        "buckets": {
            "constructive": constructive[:15],
            "deteriorating": deteriorating[:15],
        },
        "rows": rows,
        "skipped": ranking["skipped"],
        "benchmarks": {
            b: {
                "ret_1d": f.get("ret_1d"), "ret_5d": f.get("ret_5d"),
                "ret_63d": f.get("ret_63d"), "ret_252d": f.get("ret_252d"),
                "price": f.get("price"),
            } for b, f in bench_factors.items()
        },
    })
    write_json("movers.json", mover_data)
    write_json("calendar.json", {"as_of": iso(now), "events": calendar[:220]})
    write_json("news.json", {
        "as_of": iso(now),
        "articles": [
            {k: a.get(k) for k in
             ("source", "title", "url", "published_iso", "raw_score", "tickers", "hard")}
            for a in news_data.get("articles", [])[:220]
        ],
    })
    write_json("health.json", {
        "as_of": iso(now),
        "runtime_seconds": round(time.time() - started, 1),
        "demo": args.demo,
        "price_coverage": f"{len(price_factors)}/{len(tickers)}",
        "missing_prices": sorted(set(tickers) - set(price_factors)),
        "edgar_coverage": len(edgar_data.get("filings", {})),
        "trials_coverage": len(trial_data.get("by_ticker", {})),
        # How many companies the registry actually returned matching studies
        # for. trials_coverage counts attempts; this counts successes, and a
        # live run showing 0 here means the trial factors are empty.
        "trials_matched_companies": trial_data.get("matched_companies", 0),
        "trials_rejected_studies": trial_data.get("rejected_studies", 0),
        "news_articles": len(news_data.get("articles", [])),
        "ineligible": ranking["skipped"],
    })

    log.info(
        "done in %.1fs — %d constructive, %d deteriorating, %d neutral",
        time.time() - started, len(constructive), len(deteriorating),
        ranking.get("counts", {}).get("neutral", 0),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
