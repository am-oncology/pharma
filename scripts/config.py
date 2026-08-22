"""Central configuration for Biotech Radar.

Everything tunable lives here: data sources, the event lexicon used to read
headlines, and the factor weights used to build the composite score.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CACHE = ROOT / ".cache"

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
# The SEC requires a descriptive User-Agent with contact details. Set
# RADAR_CONTACT as a repository secret. Requests without it get rate-limited
# or blocked outright.
CONTACT = os.environ.get("RADAR_CONTACT", "biotech-radar (set RADAR_CONTACT secret)")
USER_AGENT = f"biotech-radar/1.0 ({CONTACT})"
# Several sector newswires sit behind CDNs that reject non-browser agents with
# a 403. The SEC requires the descriptive agent above, so these must stay
# separate: identify honestly to the regulator, look like a browser to the
# publishers that insist on it.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT = 30
HTTP_RETRIES = 3
SEC_RATE_LIMIT = 0.15  # seconds between SEC requests; their ceiling is 10/sec

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
CTGOV_API = "https://clinicaltrials.gov/api/v2/studies"
SEC_TICKER_MAP = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SEC_COMPANYCONCEPT = (
    "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/us-gaap/{tag}.json"
)
OPENFDA_DRUGSFDA = "https://api.fda.gov/drug/drugsfda.json"
OPENFDA_ENFORCEMENT = "https://api.fda.gov/drug/enforcement.json"
PUBMED_SEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_SUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"

# Optional keys. The pipeline degrades gracefully without them.
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "")
NCBI_KEY = os.environ.get("NCBI_API_KEY", "")

# ---------------------------------------------------------------------------
# News sources. Sector newswires plus regulator feeds.
# ---------------------------------------------------------------------------
RSS_FEEDS = [
    ("FierceBiotech", "https://www.fiercebiotech.com/rss/xml"),
    ("FiercePharma", "https://www.fiercepharma.com/rss/xml"),
    ("Endpoints News", "https://endpts.com/feed/"),
    ("STAT News", "https://www.statnews.com/feed/"),
    ("BioSpace", "https://www.biospace.com/rss/news/"),
    ("pharmaphorum", "https://pharmaphorum.com/feed"),
    ("Drugs.com New Approvals", "https://www.drugs.com/feeds/new_drug_approvals.xml"),
    ("FDA Press Releases", "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml"),
    ("FDA Drug Safety", "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/medwatch/rss.xml"),
    ("EMA News", "https://www.ema.europa.eu/en/rss.xml"),
    ("Nature Biotechnology", "https://www.nature.com/nbt.rss"),
    ("NEJM Current Issue", "https://www.nejm.org/action/showFeed?jc=nejm&type=etoc&feed=rss"),
    ("Lancet Oncology", "https://www.thelancet.com/rssfeed/lanonc_current.xml"),
    ("JCO Latest", "https://ascopubs.org/action/showFeed?jc=jco&type=etoc&feed=rss"),
]

# PubMed queries run per-company are expensive; these run sector-wide and are
# matched back to companies by name.
PUBMED_TOPIC_QUERIES = [
    "(phase 3[Title/Abstract]) AND (oncology[Title/Abstract]) AND randomized[Publication Type]",
    "(overall survival[Title/Abstract]) AND (phase III[Title/Abstract])",
]

# Conferences that reliably move the sector. Used to build the catalyst
# calendar; update the dates once a year.
CONFERENCES = [
    {"name": "JP Morgan Healthcare", "start": "2027-01-11", "end": "2027-01-14", "tags": ["guidance", "M&A"]},
    {"name": "AACR Annual Meeting", "start": "2027-04-16", "end": "2027-04-21", "tags": ["oncology", "early-phase"]},
    {"name": "ASCO Annual Meeting", "start": "2027-06-04", "end": "2027-06-08", "tags": ["oncology", "phase-3"]},
    {"name": "EHA Congress", "start": "2027-06-10", "end": "2027-06-13", "tags": ["haematology"]},
    {"name": "ESMO Congress", "start": "2026-10-16", "end": "2026-10-20", "tags": ["oncology", "phase-3"]},
    {"name": "SABCS", "start": "2026-12-08", "end": "2026-12-12", "tags": ["breast"]},
    {"name": "ASH Annual Meeting", "start": "2026-12-05", "end": "2026-12-08", "tags": ["haematology"]},
    {"name": "SNO Annual Meeting", "start": "2026-11-19", "end": "2026-11-22", "tags": ["neuro-oncology"]},
]

# ---------------------------------------------------------------------------
# Event lexicon.
#
# weight  : contribution to news sentiment, roughly -3..+3
# hard    : if True, this alone forces the stock into a flagged bucket
#           regardless of what the price factors say
# kind    : grouping used for display
# ---------------------------------------------------------------------------
LEXICON = [
    # --- hard negatives: these end theses ---
    ("clinical hold",                 -3.0, True,  "regulatory"),
    ("complete response letter",      -3.0, True,  "regulatory"),
    ("crl from fda",                  -3.0, True,  "regulatory"),
    ("fda rejects",                   -3.0, True,  "regulatory"),
    ("fda declines",                  -2.8, True,  "regulatory"),
    ("refuse to file",                -2.8, True,  "regulatory"),
    ("did not meet the primary",      -3.0, True,  "readout"),
    ("failed to meet the primary",    -3.0, True,  "readout"),
    ("missed the primary endpoint",   -3.0, True,  "readout"),
    ("discontinu",                    -2.2, True,  "pipeline"),
    ("terminat",                      -2.2, True,  "pipeline"),
    ("halts enrollment",              -2.5, True,  "pipeline"),
    ("study was stopped for futility", -3.0, True, "readout"),
    ("stopped for futility",          -3.0, True,  "readout"),
    ("going concern",                 -3.0, True,  "financial"),
    ("chapter 11",                    -3.0, True,  "financial"),
    ("delisting",                     -2.5, True,  "financial"),
    ("withdraws application",         -2.5, True,  "regulatory"),
    ("recall",                        -1.8, True,  "safety"),
    ("patient death",                 -2.5, True,  "safety"),
    ("treatment-related death",       -2.5, True,  "safety"),
    ("safety signal",                 -2.2, True,  "safety"),
    ("black box warning",             -2.0, True,  "safety"),
    ("boxed warning",                 -2.0, True,  "safety"),
    # --- soft negatives ---
    ("adcomm voted against",          -2.5, False, "regulatory"),
    ("advisory committee voted no",   -2.5, False, "regulatory"),
    ("misses estimates",              -1.2, False, "financial"),
    ("cuts guidance",                 -1.6, False, "financial"),
    ("lowers guidance",               -1.6, False, "financial"),
    ("restructuring",                 -1.2, False, "financial"),
    ("layoffs",                       -1.2, False, "financial"),
    ("workforce reduction",           -1.2, False, "financial"),
    ("patent invalid",                -2.0, False, "legal"),
    ("loss of exclusivity",           -1.5, False, "legal"),
    ("generic entry",                 -1.3, False, "legal"),
    ("biosimilar launch",             -1.2, False, "legal"),
    ("class action",                  -1.0, False, "legal"),
    ("subpoena",                      -1.2, False, "legal"),
    ("ceo steps down",                -1.0, False, "governance"),
    ("cfo resigns",                   -1.2, False, "governance"),
    ("chief medical officer departs", -1.0, False, "governance"),
    # --- dilution: not fatal, but reliably repricing ---
    ("public offering",               -1.5, False, "dilution"),
    ("registered direct offering",    -1.6, False, "dilution"),
    ("at-the-market offering",        -1.0, False, "dilution"),
    ("pricing of",                    -1.2, False, "dilution"),
    ("shelf registration",            -0.6, False, "dilution"),
    ("convertible notes offering",    -1.0, False, "dilution"),
    # --- soft positives ---
    ("positive topline",               2.5, False, "readout"),
    ("met the primary endpoint",       3.0, False, "readout"),
    ("statistically significant",      1.8, False, "readout"),
    ("clinically meaningful",          1.2, False, "readout"),
    ("overall survival benefit",       2.5, False, "readout"),
    ("improved progression-free",      2.0, False, "readout"),
    ("interim analysis",               0.8, False, "readout"),
    ("stopped early for efficacy",     3.0, False, "readout"),
    ("breakthrough therapy designation", 2.0, False, "regulatory"),
    ("priority review",                1.8, False, "regulatory"),
    ("accelerated approval",           2.2, False, "regulatory"),
    ("fda approves",                   3.0, False, "regulatory"),
    ("fda approval",                   3.0, False, "regulatory"),
    ("ema approval",                   2.0, False, "regulatory"),
    ("chmp positive opinion",          2.0, False, "regulatory"),
    ("orphan drug designation",        1.2, False, "regulatory"),
    ("fast track designation",         1.2, False, "regulatory"),
    ("rmat designation",               1.5, False, "regulatory"),
    ("adcomm voted in favor",          2.5, False, "regulatory"),
    ("nda accepted",                   1.2, False, "regulatory"),
    ("bla accepted",                   1.2, False, "regulatory"),
    ("pdufa date",                     0.8, False, "regulatory"),
    ("to acquire",                     2.5, False, "corporate"),
    ("acquisition of",                 2.0, False, "corporate"),
    ("merger agreement",               2.5, False, "corporate"),
    ("takeover",                       2.5, False, "corporate"),
    ("licensing agreement",            1.2, False, "corporate"),
    ("strategic collaboration",        1.2, False, "corporate"),
    ("milestone payment",              1.0, False, "corporate"),
    ("upfront payment",                1.2, False, "corporate"),
    ("raises guidance",                1.8, False, "financial"),
    ("beats estimates",                1.2, False, "financial"),
    ("record revenue",                 1.2, False, "financial"),
]

# ---------------------------------------------------------------------------
# ClinicalTrials.gov status transitions worth noticing.
# ---------------------------------------------------------------------------
TRIAL_STATUS_SIGNAL = {
    "TERMINATED": (-2.5, True),
    "SUSPENDED": (-2.5, True),
    "WITHDRAWN": (-2.0, True),
    "ACTIVE_NOT_RECRUITING": (0.8, False),   # enrolment complete, readout ahead
    "COMPLETED": (1.0, False),
    "RECRUITING": (0.3, False),
    "ENROLLING_BY_INVITATION": (0.2, False),
    "NOT_YET_RECRUITING": (0.1, False),
}

# SEC forms that matter, mapped to interpretation.
SEC_FORMS = {
    "8-K": ("material event", 0.0),
    "424B5": ("priced offering", -1.5),
    "424B4": ("priced offering", -1.5),
    "S-3": ("shelf registration", -0.5),
    "S-3ASR": ("automatic shelf", -0.4),
    "S-1": ("registration", -0.8),
    "SC 13D": ("activist stake", 1.2),
    "SC 14D9": ("tender offer response", 2.0),
    "DEFM14A": ("merger proxy", 2.0),
    "10-Q": ("quarterly report", 0.0),
    "10-K": ("annual report", 0.0),
}

# ---------------------------------------------------------------------------
# Composite score weights.
#
# Each factor is winsorised then z-scored across the ranked universe before
# weighting, so these are directly comparable. They sum to 1.0 for the
# positive side; risk factors subtract.
# ---------------------------------------------------------------------------
WEIGHTS = {
    "relative_strength": 0.22,   # 3m return vs XBI — the core momentum term
    "trend": 0.14,               # position vs 50d and 200d moving averages
    "long_momentum": 0.10,       # 6m absolute return
    "accumulation": 0.08,        # volume expansion on up days
    "news_sentiment": 0.20,      # recency-decayed lexicon score
    "catalyst_proximity": 0.10,  # dated events in the next 90 days
    "trial_momentum": 0.08,      # pipeline status transitions
    "reversion_penalty": -0.06,  # punish stretched RSI in both directions
    "dilution_risk": -0.12,      # short runway + recent offering filings
    "drawdown_risk": -0.06,      # distance below 200d MA when trend is broken
}

# Buckets are assigned on the composite z-score.
BUCKETS = {
    "constructive": 0.75,    # score >= this
    "deteriorating": -0.75,  # score <= this
}

# Minimum liquidity to be ranked at all. Illiquid names produce meaningless
# signals and cannot be exited.
MIN_DOLLAR_VOLUME = 1_000_000   # 21-day median
MIN_PRICE = 1.50
MIN_HISTORY_DAYS = 150

LOOKBACK_DAYS = 800
NEWS_HALFLIFE_DAYS = 7.0
CATALYST_WINDOW_DAYS = 120
