"""SEC EDGAR ingest: filings, dilution flags and cash runway.

Cash runway is the most under-used number in small-cap biotech. A company
with nine months of cash and a Phase III readout eleven months out is going
to raise equity before the data arrive, and the market prices that in before
you do. It is computable from XBRL facts every issuer must file.

Everything here uses the public JSON APIs on data.sec.gov. They require a
descriptive User-Agent (set RADAR_CONTACT) and tolerate about 10 requests per
second; the module self-throttles well below that.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from config import (SEC_COMPANYCONCEPT, SEC_FORMS, SEC_RATE_LIMIT,
                    SEC_SUBMISSIONS, SEC_TICKER_MAP)
from util import days_between, fetch_json, parse_date, utcnow

log = logging.getLogger("radar.edgar")

CASH_TAGS = [
    "CashAndCashEquivalentsAtCarryingValue",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
]
INVESTMENT_TAGS = [
    "ShortTermInvestments",
    "MarketableSecuritiesCurrent",
    "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
]
BURN_TAG = "NetCashProvidedByUsedInOperatingActivities"

DILUTION_FORMS = {"424B5", "424B4", "424B3", "S-1", "S-1/A", "S-3", "S-3ASR", "S-3/A"}
DEAL_FORMS = {"SC 13D", "SC 14D9", "DEFM14A", "SC TO-T", "SC 13E3"}


def ticker_to_cik() -> dict[str, int]:
    """SEC's official ticker-to-CIK map. Cached for a day."""
    payload = fetch_json(SEC_TICKER_MAP, cache_hours=24)
    if not payload:
        log.warning("could not load SEC ticker map; EDGAR features disabled")
        return {}
    out: dict[str, int] = {}
    values = payload.values() if isinstance(payload, dict) else payload
    for row in values:
        if isinstance(row, dict) and row.get("ticker"):
            out[str(row["ticker"]).upper()] = int(row["cik_str"])
    log.info("SEC ticker map: %d entries", len(out))
    return out


def recent_filings(cik: int, days: int = 120) -> list[dict]:
    """Filings from the last `days`, annotated with interpretation."""
    payload = fetch_json(SEC_SUBMISSIONS.format(cik=cik), cache_hours=4)
    time.sleep(SEC_RATE_LIMIT)
    if not payload:
        return []
    recent = (payload.get("filings", {}) or {}).get("recent", {}) or {}
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])
    descriptions = recent.get("primaryDocDescription", [])

    now = utcnow()
    out: list[dict] = []
    for i, form in enumerate(forms):
        filed = parse_date(dates[i] if i < len(dates) else None)
        if not filed or days_between(now, filed) > days:
            continue
        accession = (accessions[i] if i < len(accessions) else "").replace("-", "")
        doc = docs[i] if i < len(docs) else ""
        label, weight = SEC_FORMS.get(form, ("filing", 0.0))
        if form in DILUTION_FORMS and weight == 0.0:
            weight = -0.8
        out.append({
            "form": form,
            "label": label,
            "filed": filed.strftime("%Y-%m-%d"),
            "days_ago": round(days_between(now, filed)),
            "description": (descriptions[i] if i < len(descriptions) else "") or label,
            "weight": weight,
            "dilutive": form in DILUTION_FORMS,
            "deal": form in DEAL_FORMS,
            "url": (
                f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{doc}"
                if accession and doc else
                f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik:010d}"
            ),
        })
    return out


def _concept_values(cik: int, tag: str) -> list[dict]:
    url = SEC_COMPANYCONCEPT.format(cik=cik, tag=tag)
    payload = fetch_json(url, cache_hours=24, tolerate=(404,))
    time.sleep(SEC_RATE_LIMIT)
    if not payload:
        return []
    return (payload.get("units", {}) or {}).get("USD", []) or []


def _latest_instant(cik: int, tags: list[str]) -> tuple[float, str] | None:
    """Most recent balance-sheet value across a list of candidate tags."""
    best: tuple[float, str] | None = None
    best_date = None
    for tag in tags:
        for entry in _concept_values(cik, tag):
            end = parse_date(entry.get("end"))
            val = entry.get("val")
            if end is None or val is None:
                continue
            if best_date is None or end > best_date:
                best_date, best = end, (float(val), end.strftime("%Y-%m-%d"))
    return best


def quarterly_burn(cik: int) -> tuple[float, int] | None:
    """Average quarterly operating cash burn from the last four quarters.

    Operating cash flow is reported cumulatively within a fiscal year, so
    quarterly figures have to be recovered by differencing consecutive
    year-to-date values. Only genuine ~90-day windows are kept.
    """
    entries = _concept_values(cik, BURN_TAG)
    if not entries:
        return None
    periods = []
    for entry in entries:
        start, end = parse_date(entry.get("start")), parse_date(entry.get("end"))
        val = entry.get("val")
        if not start or not end or val is None:
            continue
        span = (end - start).days
        if 80 <= span <= 100:
            periods.append((end, float(val)))
    if len(periods) < 2:
        # Fall back to annual figures divided by four.
        annual = [
            (parse_date(e["end"]), float(e["val"]))
            for e in entries
            if e.get("start") and e.get("end") and e.get("val") is not None
            and 350 <= (parse_date(e["end"]) - parse_date(e["start"])).days <= 380
        ]
        if not annual:
            return None
        annual.sort(key=lambda p: p[0], reverse=True)
        return annual[0][1] / 4.0, 1

    periods.sort(key=lambda p: p[0], reverse=True)
    seen, unique = set(), []
    for end, val in periods:
        key = end.strftime("%Y-%m")
        if key not in seen:
            seen.add(key)
            unique.append(val)
    window = unique[:4]
    return sum(window) / len(window), len(window)


def cash_position(cik: int) -> dict[str, Any]:
    """Cash, burn and implied runway in months."""
    result: dict[str, Any] = {
        "cash": None, "cash_date": None, "quarterly_burn": None,
        "runway_months": None, "quarters_used": 0,
    }
    cash_entry = _latest_instant(cik, CASH_TAGS)
    if not cash_entry:
        return result
    cash, cash_date = cash_entry
    investments = _latest_instant(cik, INVESTMENT_TAGS)
    if investments and investments[1] == cash_date:
        cash += investments[0]

    result["cash"] = cash
    result["cash_date"] = cash_date

    burn = quarterly_burn(cik)
    if not burn:
        return result
    quarterly, n = burn
    result["quarterly_burn"] = quarterly
    result["quarters_used"] = n

    # Negative operating cash flow means the company is consuming cash.
    if quarterly < 0:
        monthly = abs(quarterly) / 3.0
        if monthly > 0:
            elapsed = 0.0
            asof = parse_date(cash_date)
            if asof:
                elapsed = max(days_between(utcnow(), asof) / 30.44, 0.0)
            gross = cash / monthly
            result["runway_months"] = round(max(gross - elapsed, 0.0), 1)
            result["reporting_lag_months"] = round(elapsed, 1)
    else:
        result["runway_months"] = 999.0  # cash generative
    return result


def dilution_risk(cash: dict[str, Any], filings: list[dict]) -> tuple[float, list[str]]:
    """Convert runway and filing history into a 0..3 risk score plus reasons.

    Higher is worse. The scale is deliberately coarse — this is a triage flag,
    not a valuation input.
    """
    score, reasons = 0.0, []
    runway = cash.get("runway_months")

    if runway is not None and runway < 900:
        if runway < 6:
            score += 3.0
            reasons.append(f"under 6 months of cash at current burn ({runway:.0f}m)")
        elif runway < 12:
            score += 2.0
            reasons.append(f"roughly {runway:.0f} months of cash — a raise is likely")
        elif runway < 18:
            score += 1.0
            reasons.append(f"{runway:.0f} months of cash")
        elif runway > 36:
            score -= 0.5
            reasons.append(f"well funded ({runway:.0f} months)")
    elif runway == 999.0:
        score -= 1.0
        reasons.append("operations are cash generative")

    priced = [f for f in filings if f["form"].startswith("424B") and f["days_ago"] <= 90]
    shelf = [f for f in filings if f["form"].startswith("S-3") and f["days_ago"] <= 90]
    if priced:
        score += 1.5
        reasons.append(f"priced an offering {priced[0]['days_ago']}d ago")
    elif shelf:
        score += 0.5
        reasons.append("filed a shelf registration in the last quarter")

    return max(score, -1.0), reasons


def ingest(companies: list[dict]) -> dict[str, Any]:
    """Run the EDGAR pipeline across the universe."""
    cikmap = ticker_to_cik()
    if not cikmap:
        return {"filings": {}, "cash": {}, "dilution": {}, "cik": {}}

    filings: dict[str, list[dict]] = {}
    cash: dict[str, dict] = {}
    dilution: dict[str, dict] = {}
    ciks: dict[str, int] = {}

    for company in companies:
        ticker = company["ticker"]
        cik = company.get("cik") or cikmap.get(ticker.upper())
        if not cik:
            log.debug("%s: no CIK (likely a foreign issuer filing 20-F, or an ADR)", ticker)
            continue
        ciks[ticker] = cik
        try:
            f = recent_filings(cik)
            c = cash_position(cik)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: EDGAR lookup failed: %s", ticker, exc)
            continue
        filings[ticker] = f
        cash[ticker] = c
        score, reasons = dilution_risk(c, f)
        dilution[ticker] = {"score": score, "reasons": reasons}
        log.debug("%s: %d filings, runway=%s", ticker, len(f), c.get("runway_months"))

    log.info("EDGAR: covered %d/%d companies", len(filings), len(companies))
    return {"filings": filings, "cash": cash, "dilution": dilution, "cik": ciks}
