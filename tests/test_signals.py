"""Tests for the factor maths. Run: python -m pytest tests/ -q"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import signals as S  # noqa: E402
import news as N  # noqa: E402
import edgar as E  # noqa: E402


def frame(closes, volumes=None):
    n = len(closes)
    close = pd.Series(closes, dtype=float,
                      index=pd.bdate_range("2024-01-01", periods=n))
    vol = pd.Series(volumes if volumes is not None else [1e6] * n,
                    dtype=float, index=close.index)
    return pd.DataFrame({
        "open": close, "high": close * 1.01, "low": close * 0.99,
        "close": close, "volume": vol,
    })


# --- returns and trend -----------------------------------------------------
def test_pct_return_exact():
    f = frame([100, 110, 121])
    assert S.pct_return(f["close"], 1) == pytest.approx(0.10)
    assert S.pct_return(f["close"], 2) == pytest.approx(0.21)


def test_pct_return_insufficient_history_is_nan():
    assert np.isnan(S.pct_return(frame([100, 101])["close"], 50))


def test_sma_gap_sign():
    rising = frame(list(np.linspace(10, 20, 120)))
    falling = frame(list(np.linspace(20, 10, 120)))
    assert S.sma_gap(rising["close"], 50) > 0
    assert S.sma_gap(falling["close"], 50) < 0


# --- RSI -------------------------------------------------------------------
def test_rsi_bounds_and_extremes():
    up = frame(list(np.linspace(10, 40, 100)))
    down = frame(list(np.linspace(40, 10, 100)))
    assert S.rsi(up["close"]) > 90
    assert S.rsi(down["close"]) < 10
    assert 0 <= S.rsi(frame(list(np.random.default_rng(1).normal(50, 2, 200)))["close"]) <= 100


def test_rsi_flat_series_is_neutral():
    assert S.rsi(frame([25.0] * 60)["close"]) == pytest.approx(50.0)


# --- volatility ------------------------------------------------------------
def test_realised_vol_scales_with_noise():
    rng = np.random.default_rng(3)
    quiet = frame(list(100 * np.exp(np.cumsum(rng.normal(0, 0.004, 300)))))
    loud = frame(list(100 * np.exp(np.cumsum(rng.normal(0, 0.040, 300)))))
    assert S.realised_vol(loud["close"]) > 4 * S.realised_vol(quiet["close"])


def test_atr_is_positive_fraction():
    a = S.atr_pct(frame(list(np.linspace(10, 30, 100))))
    assert 0 < a < 1


# --- volume ----------------------------------------------------------------
def test_volume_zscore_detects_spike():
    vols = [1e6] * 120 + [9e6] * 5
    f = frame(list(np.linspace(10, 12, 125)), vols)
    assert S.volume_zscore(f["volume"]) > 3


def test_accumulation_bounds():
    up = frame(list(np.linspace(10, 20, 60)))
    down = frame(list(np.linspace(20, 10, 60)))
    assert S.accumulation(up) == pytest.approx(0.5, abs=0.05)
    assert S.accumulation(down) == pytest.approx(-0.5, abs=0.05)


# --- drawdown and gaps -----------------------------------------------------
def test_drawdown_measures_distance_below_peak():
    # Needs >= 20 observations; the guard below that returns NaN by design.
    f = frame([10.0] * 20 + [30.0] + [15.0])
    assert S.drawdown_from_high(f["close"]) == pytest.approx(-0.5)


def test_drawdown_is_zero_at_a_new_high():
    f = frame(list(np.linspace(10, 40, 60)))
    assert S.drawdown_from_high(f["close"]) == pytest.approx(0.0)


def test_drawdown_short_history_returns_nan():
    assert np.isnan(S.drawdown_from_high(frame([10, 20, 30, 15])["close"]))


def test_gap_events_counts_binary_moves():
    closes = [100.0] * 50
    closes.append(180.0)   # +80% readout
    closes.append(90.0)    # -50% reversal
    assert S.gap_events(frame(closes)["close"]) == 2


def test_gap_events_ignores_normal_drift():
    assert S.gap_events(frame(list(np.linspace(100, 130, 200)))["close"]) == 0


# --- beta / alpha ----------------------------------------------------------
def test_beta_recovers_known_leverage():
    rng = np.random.default_rng(11)
    bench_ret = rng.normal(0, 0.01, 300)
    stock_ret = 2.0 * bench_ret
    bench = frame(list(100 * np.exp(np.cumsum(bench_ret))))
    stock = frame(list(100 * np.exp(np.cumsum(stock_ret))))
    beta, _ = S.beta_and_alpha(stock["close"], bench["close"])
    assert beta == pytest.approx(2.0, abs=0.15)


def test_relative_strength_is_zero_for_identical_series():
    f = frame(list(np.linspace(10, 20, 300)))
    out = S.compute_factors(f, f)
    assert out["rs_63d"] == pytest.approx(0.0, abs=1e-9)


# --- cross-sectional -------------------------------------------------------
def test_zscore_centres_and_scales():
    z = S.zscore(np.array([1.0, 2, 3, 4, 5, 6, 7, 8, 9, 10]))
    assert abs(z.mean()) < 0.3
    assert z[0] < 0 < z[-1]


def test_zscore_handles_nan_as_neutral():
    z = S.zscore(np.array([1.0, 2, 3, np.nan, 5, 6, 7]))
    assert np.isfinite(z).all()
    assert z[3] == 0.0


def test_zscore_constant_input_does_not_divide_by_zero():
    assert (S.zscore(np.array([5.0] * 10)) == 0).all()


def test_winsorise_clips_outlier():
    v = np.array([1.0] * 20 + [1000.0])
    assert S.winsorise(v).max() < 1000


# --- lexicon ---------------------------------------------------------------
def test_lexicon_reads_failed_endpoint_as_severe():
    score, hits = N.score_text("Company X: Phase 3 study did not meet the primary endpoint")
    assert score <= -2.0
    assert any(h["hard"] for h in hits)


def test_lexicon_reads_approval_as_positive():
    score, _ = N.score_text("FDA approves novel therapy for advanced disease")
    assert score >= 2.0


def test_negation_guard_overrides_positive_phrasing():
    # "statistically significant" is positive in isolation; the negation must win.
    score, _ = N.score_text(
        "Trial failed to meet its primary endpoint despite a statistically significant trend"
    )
    assert score <= -2.0


def test_clinical_hold_is_hard_flagged():
    _, hits = N.score_text("FDA places pivotal trial on clinical hold")
    assert any(h["hard"] and h["weight"] < 0 for h in hits)


def test_offering_is_negative_but_not_hard():
    score, hits = N.score_text("Company announces pricing of public offering of common stock")
    assert score < 0
    assert not any(h["hard"] for h in hits)


# --- name matching ---------------------------------------------------------
def test_mentions_matches_full_name():
    from util import mentions
    assert mentions("Revolution Medicines reports data", "Revolution Medicines", "RVMD")


def test_mentions_rejects_bare_english_word_ticker():
    # 'RARE' and 'BEAM' are ordinary words; matching them bare would be noise.
    from util import mentions
    assert not mentions("A rare beam of light", "Ultragenyx", "RARE")


def test_mentions_accepts_cashtag():
    from util import mentions
    assert mentions("Big move in $RARE today", "Ultragenyx", "RARE")


# --- dilution --------------------------------------------------------------
def test_short_runway_scores_high_risk():
    score, reasons = E.dilution_risk({"runway_months": 5.0}, [])
    assert score >= 3.0 and reasons


def test_cash_generative_scores_negative_risk():
    score, _ = E.dilution_risk({"runway_months": 999.0}, [])
    assert score < 0


def test_recent_priced_offering_adds_risk():
    base, _ = E.dilution_risk({"runway_months": 24.0}, [])
    with_offering, _ = E.dilution_risk(
        {"runway_months": 24.0},
        [{"form": "424B5", "days_ago": 10, "dilutive": True, "deal": False}],
    )
    assert with_offering > base


# --- integration -----------------------------------------------------------
def test_compute_factors_returns_finite_core_fields():
    rng = np.random.default_rng(5)
    f = frame(list(100 * np.exp(np.cumsum(rng.normal(0.0005, 0.02, 400)))))
    out = S.compute_factors(f, f)
    for key in ("price", "trend", "rsi14", "vol_ann", "ret_63d"):
        assert np.isfinite(out[key]), key


def test_catalyst_proximity_is_monotone_in_nearness():
    from score import catalyst_proximity
    near = catalyst_proximity([{"days_out": 10, "phase_weight": 1.0}], None)
    far = catalyst_proximity([{"days_out": 200, "phase_weight": 1.0}], None)
    assert near > far >= 0


# ---------------------------------------------------------------------------
# Regressions found while hardening the live path
# ---------------------------------------------------------------------------
def test_sponsor_matching_accepts_corporate_variants():
    """The registry is inconsistent about corporate forms."""
    from trials import _sponsor_matches
    assert _sponsor_matches("Novartis", "Novartis Pharmaceuticals")
    assert _sponsor_matches("Eli Lilly", "Eli Lilly and Company")
    assert _sponsor_matches("AstraZeneca", "AstraZeneca AB")
    assert _sponsor_matches("Vertex Pharmaceuticals", "Vertex Pharmaceuticals Incorporated")


def test_sponsor_matching_rejects_unrelated_sponsors():
    """The guard that stops another company's pipeline being ingested.

    The v2 API ignores unknown parameters silently, so a mis-specified filter
    returns a normal-looking page of unrelated studies rather than an error.
    """
    from trials import _sponsor_matches
    assert not _sponsor_matches("Pfizer", "Shandong University")
    assert not _sponsor_matches("Pfizer", "Federal University of Minas Gerais")
    assert not _sponsor_matches("Moderna", "Hospices Civils de Lyon")
    # Shared corporate noise words alone must not produce a match.
    assert not _sponsor_matches("Alnylam Pharmaceuticals", "Ionis Pharmaceuticals")
    assert not _sponsor_matches("Roche", "")


def test_synthetic_price_index_survives_non_business_days():
    """bdate_range(end=...) drops a weekend endpoint; the Saturday cron hit it."""
    import pandas as pd
    from build import _synthetic_prices
    for anchor in ("2026-08-22", "2026-08-23", "2026-08-24"):  # Sat, Sun, Mon
        original = pd.Timestamp.today
        pd.Timestamp.today = staticmethod(lambda a=anchor: pd.Timestamp(a))
        try:
            frames = _synthetic_prices(["AAA", "BBB"], days=120)
        finally:
            pd.Timestamp.today = original
        for ticker, frame in frames.items():
            assert len(frame) > 0, f"{ticker} empty at {anchor}"
            assert frame["close"].notna().all()
            assert not frame.index.duplicated().any()


# ---------------------------------------------------------------------------
# API contract guards
#
# These lock in facts about third-party APIs that were verified against the
# live specs. They exist because both failure modes below are silent: the
# request succeeds, the payload looks normal, and the factor quietly empties.
# ---------------------------------------------------------------------------
def test_ctgov_fields_are_piece_names_not_json_paths():
    """CTgov v2 `fields` takes piece names (NCTId), not dotted JSON paths.

    Dotted paths are rejected and the API fails soft, so the pipeline would
    receive well-formed pages with every field missing and conclude the
    sponsor has no trials.
    """
    import trials
    assert "." not in trials.FIELDS, (
        "fields must be piece names such as NCTId, not "
        "protocolSection.identificationModule.nctId"
    )
    for required in ("NCTId", "OverallStatus", "LeadSponsorName",
                     "PrimaryCompletionDate"):
        assert required in trials.FIELDS


def test_price_ingest_pins_auto_adjust():
    """yfinance flipped the auto_adjust default from False to True.

    The ingest relies on an explicit adj_close column to build its return
    series, so the parameter must stay pinned rather than inherited.
    """
    import inspect

    import prices
    src = inspect.getsource(prices.download)
    assert "auto_adjust=False" in src
    assert src.count("auto_adjust=False") >= 2, "batch and per-ticker paths must both pin it"
