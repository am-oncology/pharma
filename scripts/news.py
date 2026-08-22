"""Qualitative ingest: trade press, regulators, journals.

The scoring here is a weighted keyword lexicon with exponential recency
decay. It is deliberately not a neural sentiment model, for two reasons.
First, general-purpose sentiment models read "Phase III trial fails to meet
primary endpoint" as mildly negative prose when it is in fact a company-ending
event. Second, a lexicon is auditable: when the radar flags something you can
see exactly which phrase in which headline caused it, and correct the lexicon
when it is wrong. An opaque score you cannot interrogate is worse than no
score.

The obvious limitation is that a lexicon cannot read a hazard ratio. It will
tell you a readout happened; it cannot tell you whether a HR of 0.87 in that
population is commercially meaningful. That judgement is yours.
"""
from __future__ import annotations

import logging
import math
import re
from typing import Any

from config import (LEXICON, NEWS_HALFLIFE_DAYS, NCBI_KEY, OPENFDA_DRUGSFDA,
                    PUBMED_SEARCH, PUBMED_SUMMARY, PUBMED_TOPIC_QUERIES,
                    RSS_FEEDS, FINNHUB_KEY)
from util import (clean_html, days_between, fetch_json, fetch_text, iso,
                  mentions, parse_date, utcnow)

log = logging.getLogger("radar.news")


# ---------------------------------------------------------------------------
# Feed parsing
# ---------------------------------------------------------------------------
def _parse_feed(xml: str, source: str) -> list[dict]:
    """Parse RSS or Atom. Uses feedparser when available, regex otherwise."""
    items: list[dict] = []
    try:
        import feedparser
        parsed = feedparser.parse(xml)
        for entry in parsed.entries:
            published = (entry.get("published") or entry.get("updated")
                         or entry.get("pubDate") or "")
            items.append({
                "source": source,
                "title": clean_html(entry.get("title", "")),
                "summary": clean_html(entry.get("summary", ""))[:600],
                "url": entry.get("link", ""),
                "published": published,
            })
        return items
    except ImportError:
        pass

    blocks = re.findall(r"<(?:item|entry)\b.*?</(?:item|entry)>", xml, flags=re.S | re.I)
    for block in blocks:
        def grab(tag: str) -> str:
            m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", block, flags=re.S | re.I)
            return clean_html(m.group(1)) if m else ""
        link = grab("link")
        if not link:
            m = re.search(r'<link[^>]*href="([^"]+)"', block, flags=re.I)
            link = m.group(1) if m else ""
        items.append({
            "source": source,
            "title": grab("title"),
            "summary": (grab("description") or grab("summary"))[:600],
            "url": link,
            "published": grab("pubDate") or grab("published") or grab("updated"),
        })
    return items


def fetch_feeds() -> list[dict]:
    """Pull every configured feed. Dead feeds are skipped, not fatal."""
    articles: list[dict] = []
    for source, url in RSS_FEEDS:
        xml = fetch_text(url, cache_hours=0.5)
        if not xml:
            log.warning("feed unavailable: %s", source)
            continue
        parsed = _parse_feed(xml, source)
        articles.extend(parsed)
        log.info("%-28s %3d items", source, len(parsed))
    return articles


def fetch_company_news(tickers: list[str], days: int = 21) -> list[dict]:
    """Company-tagged headlines from Finnhub, if a key is configured.

    This is the only source that gives a reliable ticker mapping rather than
    requiring name matching, so it is worth the free-tier key.
    """
    if not FINNHUB_KEY:
        return []
    from datetime import timedelta
    now = utcnow()
    start = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    end = now.strftime("%Y-%m-%d")
    out: list[dict] = []
    for ticker in tickers:
        payload = fetch_json(
            "https://finnhub.io/api/v1/company-news",
            params={"symbol": ticker, "from": start, "to": end, "token": FINNHUB_KEY},
            cache_hours=2,
        )
        if not isinstance(payload, list):
            continue
        for item in payload[:40]:
            ts = item.get("datetime")
            out.append({
                "source": item.get("source", "Finnhub"),
                "title": item.get("headline", ""),
                "summary": (item.get("summary", "") or "")[:600],
                "url": item.get("url", ""),
                "published": iso(
                    __import__("datetime").datetime.fromtimestamp(ts, tz=__import__("datetime").timezone.utc)
                ) if ts else "",
                "ticker": ticker,
            })
    log.info("Finnhub company news: %d items", len(out))
    return out


def fetch_pubmed(max_per_query: int = 40) -> list[dict]:
    """Recent late-phase literature. Matched to companies by sponsor name."""
    out: list[dict] = []
    for query in PUBMED_TOPIC_QUERIES:
        params = {
            "db": "pubmed", "term": query, "retmax": max_per_query,
            "retmode": "json", "sort": "date",
        }
        if NCBI_KEY:
            params["api_key"] = NCBI_KEY
        search = fetch_json(PUBMED_SEARCH, params=params, cache_hours=12)
        ids = ((search or {}).get("esearchresult", {}) or {}).get("idlist", []) or []
        if not ids:
            continue
        sparams = {"db": "pubmed", "id": ",".join(ids), "retmode": "json"}
        if NCBI_KEY:
            sparams["api_key"] = NCBI_KEY
        summary = fetch_json(PUBMED_SUMMARY, params=sparams, cache_hours=12)
        result = (summary or {}).get("result", {}) or {}
        for pmid in ids:
            rec = result.get(pmid)
            if not isinstance(rec, dict):
                continue
            out.append({
                "source": rec.get("fulljournalname") or rec.get("source") or "PubMed",
                "title": rec.get("title", ""),
                "summary": "",
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                "published": rec.get("sortpubdate") or rec.get("pubdate") or "",
                "kind": "literature",
            })
    log.info("PubMed: %d records", len(out))
    return out


def fetch_fda_approvals(days: int = 180) -> list[dict]:
    """Recent submissions and approvals from openFDA's Drugs@FDA dataset."""
    from datetime import timedelta
    since = (utcnow() - timedelta(days=days)).strftime("%Y%m%d")
    payload = fetch_json(
        OPENFDA_DRUGSFDA,
        params={
            "search": f"submissions.submission_status_date:[{since}+TO+30991231]",
            "limit": 100,
        },
        cache_hours=12,
        tolerate=(404,),
    )
    results = (payload or {}).get("results", []) or []
    out: list[dict] = []
    for rec in results:
        sponsor = rec.get("sponsor_name", "")
        products = rec.get("products", []) or []
        brand = products[0].get("brand_name", "") if products else ""
        for sub in rec.get("submissions", []) or []:
            status = (sub.get("submission_status") or "").upper()
            date = parse_date(sub.get("submission_status_date"))
            if not date:
                continue
            out.append({
                "source": "FDA Drugs@FDA",
                "sponsor": sponsor,
                "title": (
                    f"{brand or rec.get('application_number', 'Application')} — "
                    f"{sub.get('submission_type', '')} {sub.get('submission_number', '')} {status}"
                ).strip(),
                "summary": f"{sponsor}. Review priority: {sub.get('review_priority') or 'standard'}.",
                "url": "https://www.accessdata.fda.gov/scripts/cder/daf/",
                "published": date.strftime("%Y-%m-%d"),
                "kind": "regulatory",
                "approved": status == "AP",
            })
    log.info("openFDA: %d submission events", len(out))
    return out


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def score_text(text: str) -> tuple[float, list[dict]]:
    """Apply the lexicon to one headline. Returns (score, matched terms)."""
    low = (text or "").lower()
    total, hits = 0.0, []
    for phrase, weight, hard, kind in LEXICON:
        if phrase in low:
            total += weight
            hits.append({"phrase": phrase, "weight": weight, "hard": hard, "kind": kind})
    # Negation guard: "did not meet" flips a positive endpoint phrase.
    if re.search(r"\b(did not|failed to|does not)\s+(meet|achieve|demonstrate)", low):
        total = min(total, -2.0)
    return total, hits


def attach_tickers(articles: list[dict], companies: list[dict]) -> list[dict]:
    """Map free-text articles to tickers by company-name matching."""
    out: list[dict] = []
    for article in articles:
        blob = f"{article.get('title','')} {article.get('summary','')} {article.get('sponsor','')}"
        if article.get("ticker"):
            matched = [article["ticker"]]
        else:
            matched = [c["ticker"] for c in companies if mentions(blob, c["name"], c["ticker"])]
        if matched:
            article = dict(article)
            article["tickers"] = matched
            out.append(article)
    return out


def build(companies: list[dict]) -> dict[str, Any]:
    """Fetch everything, score it, and aggregate to per-ticker sentiment."""
    tickers = [c["ticker"] for c in companies]
    raw = fetch_feeds()
    raw += fetch_company_news(tickers)
    raw += fetch_pubmed()
    raw += fetch_fda_approvals()

    now = utcnow()
    tagged = attach_tickers(raw, companies)

    seen: set[str] = set()
    articles: list[dict] = []
    for article in tagged:
        key = (article.get("url") or article.get("title", ""))[:200]
        if key in seen:
            continue
        seen.add(key)

        published = parse_date(article.get("published"))
        age = max(days_between(now, published), 0.0) if published else 30.0
        if age > 45:
            continue

        score, hits = score_text(f"{article.get('title','')} {article.get('summary','')}")
        decay = 0.5 ** (age / NEWS_HALFLIFE_DAYS)
        article.update({
            "age_days": round(age, 1),
            "published_iso": published.strftime("%Y-%m-%d") if published else "",
            "raw_score": round(score, 2),
            "decayed_score": round(score * decay, 3),
            "hits": hits,
            "hard": any(h["hard"] for h in hits),
        })
        articles.append(article)

    articles.sort(key=lambda a: (a.get("published_iso") or ""), reverse=True)

    sentiment: dict[str, float] = {}
    hard_flags: dict[str, list[dict]] = {}
    per_ticker: dict[str, list[dict]] = {}

    for article in articles:
        for ticker in article["tickers"]:
            sentiment[ticker] = sentiment.get(ticker, 0.0) + article["decayed_score"]
            per_ticker.setdefault(ticker, []).append(article)
            if article["hard"] and article["age_days"] <= 21:
                negative = [h for h in article["hits"] if h["hard"] and h["weight"] < 0]
                if negative:
                    hard_flags.setdefault(ticker, []).append({
                        "title": article["title"][:200],
                        "url": article.get("url", ""),
                        "source": article.get("source", ""),
                        "published": article.get("published_iso", ""),
                        "triggers": [h["phrase"] for h in negative],
                        "kind": negative[0]["kind"],
                    })

    # Bound the aggregate so one company with forty mentions cannot run away.
    for ticker, value in list(sentiment.items()):
        sentiment[ticker] = float(3.0 * math.tanh(value / 4.0))

    log.info("news: %d relevant articles, %d tickers with hard flags",
             len(articles), len(hard_flags))
    return {
        "articles": articles,
        "sentiment": sentiment,
        "hard_flags": hard_flags,
        "per_ticker": per_ticker,
    }
