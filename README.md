# Biotech Radar

A static, self-updating triage board for pharma and biotech equities. It merges
price factors with clinical trial registry changes, SEC filings, and sector news,
then ranks the universe into **constructive** and **deteriorating** buckets with a
per-factor breakdown of why.

Runs entirely on GitHub: Actions builds the dataset on a schedule, Pages serves the
result. No server, no database, no cost.

---

## What it does

**Quantitative**
- Relative strength against XBI over 1m and 3m, plus OLS beta and residual alpha
- Trend position against 50-day and 200-day moving averages
- Wilder's RSI, annualised realised volatility, ATR, volatility expansion
- Volume z-score and an accumulation measure (share of volume on up days)
- Drawdown from the 52-week high, and a count of single-day moves above 15%

**Qualitative**
- ClinicalTrials.gov v2 snapshot, **diffed against the previous run** to catch
  status transitions and primary-completion date slippage
- SEC EDGAR filings, with dilution detection (424B5, S-3) and an XBRL-derived
  cash runway estimate
- Weighted event lexicon over FierceBiotech, Endpoints, STAT, BioSpace, FDA, EMA,
  Nature Biotechnology, NEJM, Lancet Oncology and JCO
- openFDA approvals, PubMed late-phase literature, optional Finnhub company news

**Output**
- Ranked constructive and deteriorating lists with plain-language reasons
- A **hard flag** system that overrides the composite entirely for clinical holds,
  complete response letters, failed primary endpoints, terminations and
  going-concern language
- Session and weekly movers, unusual volume
- A 120-day catalyst calendar of readouts, conferences and earnings
- A sortable table of every ranked name

---

## Setup

```bash
git clone <your-fork> && cd biotech-radar
pip install -r requirements.txt

python scripts/build.py --demo     # synthetic data, no network — try this first
python -m http.server 8000         # then open http://localhost:8000
```

Demo mode fills the board with **randomly generated** prices, headlines and trial
records so the interface can be reviewed offline. The tickers are real companies;
none of the numbers describe them. The page marks itself unmistakably in this
state — a red bar, a watermark across every ranked list, `[SYNTHETIC DATA]` in
the tab title, and a banner on any printout.

Once that looks right:

```bash
export RADAR_CONTACT="Your Name your.email@example.com"
python scripts/build.py --preflight # confirm every live source answers
python scripts/build.py -v          # live run, roughly 10–25 minutes
```

Run `--preflight` on any new machine or runner before trusting a board. An
unreachable source degrades quietly: the pipeline still emits a full-looking
page with that factor silently zeroed, so the failure mode it catches is a board
that looks complete and is not. CI runs it before every live build.

### Deploying

1. Push to GitHub.
2. **Settings → Pages → Source: GitHub Actions.**
3. **Settings → Secrets and variables → Actions**, add:
   - `RADAR_CONTACT` — **required.** The SEC blocks requests without a contactable
     User-Agent.
   - `FINNHUB_API_KEY` — optional, free tier. The only source that gives reliable
     ticker tagging rather than name matching.
   - `NCBI_API_KEY` — optional, free. Raises the PubMed rate limit.
4. **Actions → Update radar → Run workflow** to trigger the first build.

The schedule then runs at 07:00, 12:00 and 21:15 UTC on weekdays.

### Configuring

Everything user-facing is in `universe.json`:

| Key | Purpose |
|---|---|
| `companies` | The tickers covered. Add or remove freely. |
| `excluded.tickers` | **Ingested and displayed but never ranked.** See below. |
| `holdings.tickers` | Positions you actually hold; they get a badge. |
| `benchmarks` | Defaults to XBI for sector, SPY for market. |

Factor weights, the event lexicon, RSS feeds and conference dates are in
`scripts/config.py`. The weights are deliberately legible — change them and the
factor bars on every card change with them.

---

## Before you use this

**Check your conflict-of-interest position first.** If you are an investigator on a
trial, sit on an advisory board, or have sight of embargoed conference abstracts or
unblinded data, trading in that sponsor can be a market-abuse offence under UK MAR
regardless of intent, and the consequences are criminal rather than financial. The
`excluded.tickers` list exists precisely for this — populate it before you rely on
the board, and talk to your employer's COI lead if there is any ambiguity.

**This is not financial advice, and it is not an edge.** Every input is public and
machine-readable, and the sector is covered by specialist funds employing full-time
clinicians. If a mechanical rule over this data worked, it would already be
arbitraged out. The board's value is in surfacing the right dozen names each morning
so that your own reading of the underlying science gets applied to them.

---

## Known limitations

These are real, and the interface repeats them where they matter:

- **It cannot read a hazard ratio.** The lexicon tells you a readout happened. Whether
  an HR of 0.87 in that population is commercially meaningful is a clinical judgement.
- **Momentum barely applies.** Small and mid-cap biotech prices are step functions
  punctuated by binary readouts, not diffusion processes. The `Gaps` column counts
  single-day moves above 15% over the past year — where it is high, treat the momentum
  factors as weak evidence.
- **Backtests on this universe will lie to you.** yfinance serves currently listed
  tickers only. Acquired, delisted and bankrupt companies are simply absent, and
  biotech has an unusually high rate of all three. Survivorship bias here is large
  enough to manufacture an apparent edge on its own.
- **Catalyst proximity is variance, not direction.** An imminent Phase III readout
  means something large is about to happen, not that it will be good.
- **Data are stale by design.** Actions runs on a schedule, so figures are typically
  15–60 minutes old. Fine for catalyst-driven work, useless intraday.
- **Name matching is imperfect.** Headlines are mapped to tickers by company name,
  which misses subsidiaries and partner-announced results. `FINNHUB_API_KEY` improves
  this materially.
- **yfinance is unofficial** and occasionally changes shape. `data/health.json`
  records coverage each run so you can see it degrade.

---

## Layout

```
├── index.html              Single page, no build step
├── assets/
│   ├── theme.css           Visual system; light and dark
│   └── app.js              Rendering, factor bars, catalyst horizon, sorting
├── scripts/
│   ├── config.py           Feeds, lexicon, factor weights — start here
│   ├── util.py             Cached HTTP, JSON IO, name matching
│   ├── signals.py          Price factor maths (pure functions)
│   ├── prices.py           yfinance ingest, liquidity screening, movers
│   ├── trials.py           ClinicalTrials.gov v2 + snapshot diffing
│   ├── edgar.py            SEC filings, dilution, XBRL cash runway
│   ├── news.py             RSS, PubMed, openFDA, lexicon scoring
│   ├── score.py            Composite ranking, buckets, rationale text
│   └── build.py            Orchestrator (--demo, --quick, --no-edgar)
├── tests/
│   ├── test_signals.py     33 tests over the factor maths and lexicon
│   └── check.js            Headless render check; also asserts the
│                           synthetic-data marking is intact
├── data/                   Generated JSON, committed by Actions
└── universe.json           Tickers, exclusions, holdings
```

## Tests

```bash
python -m pytest tests/ -q      # factor maths, lexicon, dilution logic
node tests/check.js             # renders the page in jsdom, 51 assertions
```

Both run in CI before anything deploys. The render check exists to catch the failure
mode where the pipeline succeeds, the JSON looks fine, and the page is silently blank
because a key was renamed on one side only.
