"""Shared plumbing: HTTP with retry and disk cache, JSON IO, text matching."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

from config import (BROWSER_USER_AGENT, CACHE, DATA, HTTP_RETRIES,
                    HTTP_TIMEOUT, USER_AGENT)

log = logging.getLogger("radar")

_SESSION: requests.Session | None = None


def session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        s = requests.Session()
        s.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"})
        _SESSION = s
    return _SESSION


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_date(value: Any) -> datetime | None:
    """Best-effort date parsing across the formats these APIs return."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    formats = (
        "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d", "%Y-%m", "%Y", "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z", "%d %b %Y", "%B %d, %Y",
    )
    for fmt in formats:
        try:
            dt = datetime.strptime(text, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    # Trailing timezone name, e.g. "Mon, 05 Aug 2026 09:00:00 GMT"
    stripped = re.sub(r"\s+[A-Z]{3,4}$", "", text)
    if stripped != text:
        return parse_date(stripped)
    return None


def days_between(a: datetime, b: datetime) -> float:
    return (a - b).total_seconds() / 86400.0


# ---------------------------------------------------------------------------
# HTTP with disk cache
# ---------------------------------------------------------------------------
def _cache_path(url: str, params: dict | None) -> Path:
    key = hashlib.sha256(f"{url}{sorted((params or {}).items())}".encode()).hexdigest()[:24]
    return CACHE / f"{key}.json"


def fetch_json(
    url: str,
    params: dict | None = None,
    cache_hours: float = 0.0,
    headers: dict | None = None,
    tolerate: Iterable[int] = (),
) -> Any:
    """GET JSON with retry, exponential backoff and optional disk cache.

    Returns None on failure rather than raising. A single dead source should
    never take down the whole run.
    """
    CACHE.mkdir(parents=True, exist_ok=True)
    cpath = _cache_path(url, params)
    if cache_hours > 0 and cpath.exists():
        age = time.time() - cpath.stat().st_mtime
        if age < cache_hours * 3600:
            try:
                return json.loads(cpath.read_text())
            except json.JSONDecodeError:
                pass

    delay = 1.0
    for attempt in range(HTTP_RETRIES):
        try:
            resp = session().get(url, params=params, timeout=HTTP_TIMEOUT, headers=headers)
            if resp.status_code in tolerate:
                return None
            if resp.status_code == 429:
                time.sleep(delay * 4)
                delay *= 2
                continue
            resp.raise_for_status()
            payload = resp.json()
            if cache_hours > 0:
                cpath.write_text(json.dumps(payload))
            return payload
        except (requests.RequestException, json.JSONDecodeError) as exc:
            log.warning("fetch failed (%d/%d) %s: %s", attempt + 1, HTTP_RETRIES, url, exc)
            time.sleep(delay)
            delay *= 2
    return None


def fetch_text(url: str, cache_hours: float = 0.0) -> str | None:
    """GET raw text, used for RSS/Atom."""
    CACHE.mkdir(parents=True, exist_ok=True)
    cpath = _cache_path(url, None).with_suffix(".txt")
    if cache_hours > 0 and cpath.exists():
        if time.time() - cpath.stat().st_mtime < cache_hours * 3600:
            return cpath.read_text(encoding="utf-8", errors="replace")

    delay = 1.0
    for attempt in range(HTTP_RETRIES):
        try:
            headers = {"User-Agent": BROWSER_USER_AGENT} if attempt else None
            resp = session().get(url, timeout=HTTP_TIMEOUT, headers=headers)
            if resp.status_code in (403, 406, 429) and attempt == 0:
                # Very likely CDN bot filtering rather than a real outage;
                # the next attempt presents a browser agent.
                resp.raise_for_status()
            resp.raise_for_status()
            text = resp.text
            if cache_hours > 0:
                cpath.write_text(text, encoding="utf-8")
            return text
        except requests.RequestException as exc:
            log.warning("fetch_text failed (%d/%d) %s: %s", attempt + 1, HTTP_RETRIES, url, exc)
            time.sleep(delay)
            delay *= 2
    return None


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------
def write_json(name: str, payload: Any) -> Path:
    DATA.mkdir(parents=True, exist_ok=True)
    path = DATA / name
    path.write_text(json.dumps(payload, indent=1, default=str, sort_keys=False))
    log.info("wrote %s (%.1f KB)", path.name, path.stat().st_size / 1024)
    return path


def read_json(name: str, default: Any = None) -> Any:
    path = DATA / name
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return default


# ---------------------------------------------------------------------------
# Text matching
# ---------------------------------------------------------------------------
_SUFFIXES = re.compile(
    r"\b(inc|corp|corporation|co|ltd|limited|plc|llc|lp|nv|sa|ag|as|ab|oyj|"
    r"holdings?|group|company|therapeutics?|pharmaceuticals?|pharma|biosciences?|"
    r"bioscience|biotech(nology)?|sciences?|labs?|laboratories|medicines?)\b",
    re.I,
)


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text.lower()).strip()


def name_tokens(name: str) -> list[str]:
    """Distinctive tokens for a company name, with corporate suffixes removed.

    'Revolution Medicines' -> ['revolution']; 'Eli Lilly' -> ['eli', 'lilly'].
    Used to match free-text headlines back to tickers.
    """
    base = _SUFFIXES.sub(" ", normalise(name))
    base = re.sub(r"[^a-z0-9 &-]", " ", base)
    toks = [t for t in base.split() if len(t) > 2]
    return toks or [normalise(name)]


def mentions(text: str, company_name: str, ticker: str) -> bool:
    """Does this text plausibly refer to this company?

    Requires either the full distinctive name phrase or a cash-tagged /
    parenthesised ticker. Bare uppercase tickers are too noisy to trust —
    'RARE' and 'BEAM' are ordinary English words.
    """
    low = normalise(text)
    toks = name_tokens(company_name)
    phrase = " ".join(toks)
    if phrase and phrase in low:
        return True
    if len(toks) == 1 and len(toks[0]) >= 6 and toks[0] in low:
        return True
    raw = text or ""
    patterns = (
        rf"\${ticker}\b",
        rf"\(\s*(?:NASDAQ|NYSE|Nasdaq|NYSE American)\s*:\s*{ticker}\s*\)",
        rf"\bticker\s+{ticker}\b",
    )
    return any(re.search(p, raw) for p in patterns)


def clean_html(raw: str) -> str:
    text = re.sub(r"<script.*?</script>", " ", raw or "", flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " "))
    return re.sub(r"\s+", " ", text).strip()


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("yfinance").setLevel(logging.ERROR)
