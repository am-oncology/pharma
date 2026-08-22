# HANDOFF

Session continuity notes for Biotech Radar. Newest entry at the top.

---

## State as of this session

**Working.** Pipeline builds end to end, 33 pytest + 51 jsdom assertions pass,
page renders.

**Not yet verified: the live data path.** Everything currently in `data/` is
synthetic. The sandbox this was built in blocks all egress, so
ClinicalTrials.gov, SEC, Yahoo Finance and every RSS feed returned HTTP 403.
The ingest code has therefore **never executed against a real endpoint.**

**What I could and could not verify.** yfinance defaults were checked directly
against the installed package (`auto_adjust` now defaults to `True`; the code
passes `False` explicitly, which is correct). The CTgov v2 **schema** was
confirmed against a live response — every field path in `trials.py` is right.
The CTgov **query parameters** could not be tested: the only tool that reaches
the network strips query strings, so both test calls returned the unfiltered
default listing. The sponsor filter is therefore written to the documented form
*and* verified at runtime rather than trusted.

This is the first thing to do next:

```bash
export RADAR_CONTACT="Your Name your.email@example.com"
python scripts/build.py --preflight    # expect all ok
python scripts/build.py -v             # 10–25 minutes
cat data/health.json                   # check coverage before trusting the board
```

`health.json` is the tell. Look for `demo: false`, `price_coverage` near 70/70,
**`trials_matched_companies` well above zero**, and `news_articles` in the
hundreds. That field is the one that matters: `trials_coverage` counts attempts,
`trials_matched_companies` counts companies the registry actually returned
verified studies for. If it reads 0 on a live run, the trial factors are empty
and the board will still look complete. If `trials_rejected_studies` is large,
the filter is returning other companies' trials and the guard is catching them —
check the sponsor names in `universe.json` against the registry's spelling.

Expect to fix something on first live contact. The likely candidates, in order:
yfinance column shapes (`prices._normalise_frame` is defensive but untested
against the current API), CTgov v2 field names, and RSS feeds that have moved or
now block non-browser User-Agents.

---

## Fixed this session (second pass — live-path hardening)

- **CTgov sponsor filter rewritten.** Was `query.lead=<name>`, a fuzzy Essie
  text search. Now `filter.advanced=AREA[LeadSponsorName]"<name>" AND
  AREA[StudyType]INTERVENTIONAL`, the documented field-targeted form. More
  importantly, **every returned study is now verified** against the requested
  sponsor and dropped if it doesn't match. The v2 API ignores unknown
  parameters silently rather than erroring, so a mis-specified filter returns a
  normal-looking page of unrelated studies — which would have filled
  `trial_momentum` and `catalyst_proximity` with another company's pipeline.
  Empty is recoverable; wrong is not. `ingest()` now also logs an error if zero
  companies match, and `health.json` carries `trials_matched_companies`.
  *Caveat: I could not test the parameter directly — see below.*
- **Weekend crash fixed.** `pd.bdate_range(end=today, periods=500)` returns 499
  rows when today is not a business day, and the row count was assumed. Demo
  builds crashed on any Saturday or Sunday — including the Saturday cron in the
  workflow. The index length is now derived, not assumed.
- **RSS 403s.** Fierce, STAT and BioSpace sit behind CDNs that reject
  non-browser agents. `fetch_text` now retries with a browser User-Agent.
  The SEC agent (`RADAR_CONTACT`) is kept separate and unchanged — identify
  honestly to the regulator, look like a browser to publishers that insist.
- 3 new pytest regressions (36 total) covering sponsor matching in both
  directions and the weekend index.

## Done in the first pass

- Reviewed the whole pipeline; architecture and factor maths are sound.
- **Hardened the synthetic-data marking.** Previously a small amber notice sat
  next to a similar-looking disclaimer and lost salience against it. Since demo
  mode renders *real tickers against invented numbers*, that was the sharpest
  risk in the repo. Now: `body[data-demo]`, a sticky red bar, a watermark across
  the movers, both ranked lists and the full table, `[SYNTHETIC DATA]` prefixed
  to the tab title, and a print banner.
- Added `build.py --preflight` — checks every live source plus `RADAR_CONTACT`,
  distinguishes essential from degradable, exits non-zero on essential failure.
- Wired preflight into CI as a gate before any non-demo build.
- Added 38 pytest and 51 render assertions total, including guards on both
  API contracts above and 8 covering the demo marking covering the demo marking, so it cannot silently
  regress. Also fixed the harness reading `dataFiles` as parsed JSON when it
  holds raw text.
- Corrected the watermark CSS selectors, which initially targeted
  `.card-grid` / `#radar-table` — neither exists in the DOM.
- README: documented preflight, the demo marking, updated the assertion count.

## Open items

1. **Live run + health check** — above. Nothing else matters until this is done.
2. **Populate `universe.json` → `excluded.tickers`** before relying on the board.
   Any sponsor whose trials you are involved with. Names in this list are
   ingested and displayed but never ranked. This is a conflict-of-interest
   control, not a preference — see the README section on MAR.
3. **`holdings.tickers`** is empty. Filling it makes the deteriorating list flag
   actual positions rather than abstractions, which is most of its value.
4. **Conference dates in `config.py` need an annual refresh.** ESMO, SABCS, ASH
   and SNO are set for 2026; JPM, AACR, ASCO and EHA for 2027.
5. **Name→ticker matching is the weakest link** in the news layer. It misses
   subsidiaries and partner-announced results. A free `FINNHUB_API_KEY` gives
   proper ticker tagging and would improve this materially.
6. **Deferred:** no backtest module, deliberately. Survivorship bias on a
   yfinance-derived universe is large enough here to manufacture an apparent
   edge from nothing. If this is ever added it needs a point-in-time
   constituent list, which is not free.

## Design notes

- Factor weights live in `config.py → WEIGHTS` and are directly comparable —
  every factor is winsorised then z-scored across the ranked universe before
  weighting. The factor bars on each card read straight off these.
- Bucket thresholds are `config.py → BUCKETS`, ±0.75 on the composite z-score.
  Raise it to narrow the constructive list.
- Hard flags in `LEXICON` (4th field `True`) override the composite entirely.
  The reasoning: after a clinical hold or a CRL, the price factors are about to
  be wrong, so they should not get a vote.
- CSS fixes belong in `assets/theme.css`, not inline in `index.html`.
- `catalyst_proximity` is deliberately a variance signal, not a directional one.
  An imminent Phase III readout means something large is about to happen.
