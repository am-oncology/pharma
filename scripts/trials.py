"""ClinicalTrials.gov v2 ingest, with snapshot diffing.

The diff is the point. A registry entry quietly flipping from RECRUITING to
ACTIVE_NOT_RECRUITING means enrolment closed, which puts a readout on the
clock. A primary completion date sliding six months to the right is a delay
that will usually be acknowledged publicly much later, if at all. A status of
TERMINATED or SUSPENDED is a thesis-ending event that often appears in the
registry before it appears in a press release.

None of this is inside information: it is a public registry that most people
never diff. The pipeline stores a snapshot each run and reports what moved.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from config import CTGOV_API, TRIAL_STATUS_SIGNAL
from util import days_between, iso, parse_date, read_json, utcnow, write_json

log = logging.getLogger("radar.trials")

SNAPSHOT_FILE = "trials_snapshot.json"

# The `fields` parameter takes ClinicalTrials *piece names*, not dotted JSON
# paths into the response. Passing paths like
# "protocolSection.statusModule.overallStatus" is rejected, and the API's
# habit of failing soft means you get a normal-looking page with the fields
# missing rather than an error. The response is still delivered nested inside
# protocolSection either way — `fields` prunes the tree, it does not flatten
# it — so _flatten() is unchanged.
FIELDS = ",".join([
    "NCTId",
    "BriefTitle",
    "OverallStatus",
    "LastUpdateSubmitDate",
    "StartDate",
    "PrimaryCompletionDate",
    "CompletionDate",
    "WhyStopped",
    "Phase",
    "EnrollmentCount",
    "Condition",
    "InterventionName",
    "LeadSponsorName",
])


def _dig(obj: Any, *path: str, default: Any = None) -> Any:
    """Walk a nested dict safely."""
    cur = obj
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur if cur is not None else default


def _flatten(study: dict) -> dict[str, Any]:
    ps = study.get("protocolSection", study)
    phases = _dig(ps, "designModule", "phases", default=[]) or []
    conditions = _dig(ps, "conditionsModule", "conditions", default=[]) or []
    interventions = [
        i.get("name") for i in (_dig(ps, "armsInterventionsModule", "interventions", default=[]) or [])
        if isinstance(i, dict) and i.get("name")
    ]
    return {
        "nct_id": _dig(ps, "identificationModule", "nctId", default=""),
        "title": _dig(ps, "identificationModule", "briefTitle", default=""),
        "sponsor": _dig(ps, "sponsorCollaboratorsModule", "leadSponsor", "name", default=""),
        "status": (_dig(ps, "statusModule", "overallStatus", default="") or "").upper(),
        "why_stopped": _dig(ps, "statusModule", "whyStopped", default=""),
        "last_update": _dig(ps, "statusModule", "lastUpdateSubmitDate", default=""),
        "start_date": _dig(ps, "statusModule", "startDateStruct", "date", default=""),
        "primary_completion": _dig(ps, "statusModule", "primaryCompletionDateStruct", "date", default=""),
        "primary_completion_type": _dig(ps, "statusModule", "primaryCompletionDateStruct", "type", default=""),
        "completion": _dig(ps, "statusModule", "completionDateStruct", "date", default=""),
        "phases": [p.replace("PHASE", "Phase ") for p in phases],
        "enrollment": _dig(ps, "designModule", "enrollmentInfo", "count"),
        "conditions": conditions[:6],
        "interventions": interventions[:6],
    }


def _phase_weight(phases: list[str]) -> float:
    """Later-phase readouts move more money."""
    text = " ".join(phases).lower()
    if "3" in text:
        return 1.0
    if "2" in text:
        return 0.6
    if "1" in text:
        return 0.25
    return 0.35


_CORP_SUFFIXES = (
    "incorporated", "inc", "corporation", "corp", "company", "co", "limited",
    "ltd", "plc", "llc", "lp", "nv", "sa", "ag", "as", "ab", "oy", "spa",
    "holdings", "holding", "group", "pharmaceuticals", "pharmaceutical",
    "pharma", "therapeutics", "biosciences", "bioscience", "biopharma",
    "biopharmaceuticals", "laboratories", "labs", "sciences", "and",
)


def _normalise_sponsor(name: str) -> str:
    """Reduce a company name to a comparable core token string."""
    cleaned = "".join(c.lower() if (c.isalnum() or c.isspace()) else " " for c in name)
    tokens = [t for t in cleaned.split() if t and t not in _CORP_SUFFIXES]
    return " ".join(tokens)


def _sponsor_matches(wanted: str, returned: str) -> bool:
    """Does a registry lead sponsor plausibly correspond to our company?

    The registry is inconsistent about corporate forms and subsidiaries
    ("Eli Lilly and Company", "Genentech, Inc." for Roche). Compare on the
    distinctive tokens only, and require the shorter side to be contained in
    the longer so that "Novartis" matches "Novartis Pharmaceuticals" without
    matching an unrelated sponsor that merely shares a common word.
    """
    a, b = _normalise_sponsor(wanted), _normalise_sponsor(returned)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    ta, tb = set(a.split()), set(b.split())
    return bool(ta and tb and (ta <= tb or tb <= ta))


def fetch_for_sponsor(sponsor: str, max_studies: int = 220) -> tuple[list[dict], int]:
    """All interventional studies where this company is lead sponsor.

    Returns (studies, rejected_count).

    Filtering uses filter.advanced with AREA[LeadSponsorName] rather than the
    fuzzy query.lead text search, because AREA targets the field exactly. The
    result is then verified against the requested name regardless: the API
    ignores unknown parameters silently rather than erroring, so a
    mis-specified filter returns a normal-looking page of unrelated studies.
    Trusting it unverified would quietly fill trial_momentum and
    catalyst_proximity with another company's pipeline, which is worse than
    having no trial data at all.
    """
    from util import fetch_json

    collected: list[dict] = []
    rejected = 0
    token: str | None = None
    escaped = sponsor.replace('"', "")
    # If the field list is rejected we retry without it and take the full
    # payload. Heavier, but a working ingest beats a silently empty one.
    use_fields = True
    while len(collected) < max_studies:
        params = {
            "filter.advanced": (
                f'AREA[LeadSponsorName]"{escaped}" AND '
                f"AREA[StudyType]INTERVENTIONAL"
            ),
            "pageSize": 100,
            "countTotal": "false",
        }
        if use_fields:
            params["fields"] = FIELDS
        if token:
            params["pageToken"] = token
        payload = fetch_json(CTGOV_API, params=params, cache_hours=6)
        if not payload:
            if use_fields and not collected:
                log.warning("%s: field-limited query returned nothing; "
                            "retrying with the full payload", sponsor)
                use_fields = False
                continue
            break
        studies = payload.get("studies", []) or []
        # A page of studies whose nctId did not survive flattening means the
        # field list did not do what we asked, not that the sponsor has no
        # trials. Retry once unrestricted before believing the empty result.
        flattened = [_flatten(raw) for raw in studies]
        if use_fields and studies and not any(f.get("nct_id") for f in flattened):
            log.warning("%s: fields returned no identifiable studies; "
                        "retrying with the full payload", sponsor)
            use_fields = False
            token = None
            continue
        for flat in flattened:
            if not flat.get("nct_id"):
                continue
            if not _sponsor_matches(sponsor, flat.get("sponsor", "")):
                rejected += 1
                continue
            collected.append(flat)
        token = payload.get("nextPageToken")
        if not token or not studies:
            break
    if rejected:
        log.warning("%s: dropped %d study/studies with a non-matching lead sponsor",
                    sponsor, rejected)
    return collected, rejected


def ingest(companies: list[dict]) -> dict[str, Any]:
    """Fetch trials for the whole universe and diff against last run."""
    previous = read_json(SNAPSHOT_FILE, default={}) or {}
    prev_studies: dict[str, dict] = previous.get("studies", {})

    snapshot: dict[str, dict] = {}
    by_ticker: dict[str, list[dict]] = {}
    now = utcnow()
    total_rejected = 0
    matched_companies = 0

    for company in companies:
        ticker, name = company["ticker"], company["name"]
        studies, rejected = fetch_for_sponsor(name)
        total_rejected += rejected
        if not studies:
            log.debug("%s: no registry hits for '%s'", ticker, name)
        else:
            matched_companies += 1
        by_ticker[ticker] = studies
        for s in studies:
            snapshot[s["nct_id"]] = {
                "ticker": ticker,
                "status": s["status"],
                "primary_completion": s["primary_completion"],
                "last_update": s["last_update"],
                "title": s["title"][:180],
                "phases": s["phases"],
            }
        log.info("%s: %d registered trials", ticker, len(studies))

    changes = diff(prev_studies, snapshot)
    write_json(SNAPSHOT_FILE, {"as_of": iso(now), "studies": snapshot})

    catalysts = upcoming_readouts(by_ticker, now)
    scores = trial_momentum(by_ticker, changes, now)

    if companies and matched_companies == 0:
        log.error(
            "registry returned no matching studies for ANY of %d companies. "
            "Either the API is unreachable or the sponsor filter is not being "
            "applied; trial factors will be empty rather than wrong.",
            len(companies),
        )

    return {
        "by_ticker": by_ticker,
        "changes": changes,
        "catalysts": catalysts,
        "scores": scores,
        "matched_companies": matched_companies,
        "rejected_studies": total_rejected,
    }


def diff(before: dict[str, dict], after: dict[str, dict]) -> list[dict]:
    """What moved in the registry since the last run."""
    out: list[dict] = []
    for nct, new in after.items():
        old = before.get(nct)
        if old is None:
            continue  # first sighting is not a change
        if old.get("status") != new.get("status"):
            weight, hard = TRIAL_STATUS_SIGNAL.get(new["status"], (0.0, False))
            out.append({
                "type": "status",
                "nct_id": nct,
                "ticker": new["ticker"],
                "title": new["title"],
                "phases": new.get("phases", []),
                "from": old.get("status"),
                "to": new.get("status"),
                "weight": weight,
                "hard": hard,
            })
        old_pc, new_pc = old.get("primary_completion"), new.get("primary_completion")
        if old_pc and new_pc and old_pc != new_pc:
            a, b = parse_date(old_pc), parse_date(new_pc)
            if a and b:
                shift = days_between(b, a)
                if abs(shift) >= 20:
                    out.append({
                        "type": "timeline",
                        "nct_id": nct,
                        "ticker": new["ticker"],
                        "title": new["title"],
                        "phases": new.get("phases", []),
                        "from": old_pc,
                        "to": new_pc,
                        "shift_days": round(shift),
                        # A slip is bad news; a pull-forward is good news.
                        "weight": -1.4 if shift > 0 else 1.2,
                        "hard": False,
                    })
    out.sort(key=lambda c: abs(c["weight"]), reverse=True)
    return out


def upcoming_readouts(by_ticker: dict[str, list[dict]], now) -> list[dict]:
    """Trials with an estimated primary completion in the forward window.

    Primary completion is when the last patient is measured for the primary
    endpoint. Data usually follows by weeks to months, so treat this as the
    opening of a window rather than a date.
    """
    out: list[dict] = []
    for ticker, studies in by_ticker.items():
        for s in studies:
            date = parse_date(s.get("primary_completion"))
            if not date:
                continue
            delta = days_between(date, now)
            if not (-45 <= delta <= 400):
                continue
            if s["status"] in {"TERMINATED", "WITHDRAWN", "SUSPENDED"}:
                continue
            out.append({
                "ticker": ticker,
                "nct_id": s["nct_id"],
                "title": s["title"][:160],
                "phases": s["phases"],
                "phase_weight": _phase_weight(s["phases"]),
                "status": s["status"],
                "enrollment": s.get("enrollment"),
                "conditions": s.get("conditions", [])[:3],
                "date": date.strftime("%Y-%m-%d"),
                "days_out": round(delta),
                "estimated": (s.get("primary_completion_type") or "").upper() != "ACTUAL",
                "kind": "readout",
            })
    out.sort(key=lambda c: c["days_out"])
    return out


def trial_momentum(by_ticker: dict[str, list[dict]], changes: list[dict], now) -> dict[str, float]:
    """A per-ticker pipeline health score.

    Rewards late-phase trials that are progressing; punishes terminations and
    schedule slippage. Deliberately bounded so a company with 200 registry
    entries does not dominate one with 6.
    """
    raw: dict[str, float] = {}
    for ticker, studies in by_ticker.items():
        score = 0.0
        for s in studies:
            weight, _ = TRIAL_STATUS_SIGNAL.get(s["status"], (0.0, False))
            updated = parse_date(s.get("last_update"))
            recency = 1.0
            if updated:
                age = max(days_between(now, updated), 0.0)
                recency = 0.5 ** (age / 180.0)   # six-month half-life
            score += weight * _phase_weight(s["phases"]) * recency
        n = max(len(studies), 1)
        raw[ticker] = score / (n ** 0.5)   # sublinear in pipeline size

    for change in changes:
        raw[change["ticker"]] = raw.get(change["ticker"], 0.0) + change["weight"]
    return raw
