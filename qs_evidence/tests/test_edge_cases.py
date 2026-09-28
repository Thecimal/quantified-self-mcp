"""Statistical edge cases: the claim decision must reflect the limits of the statistic actually reported.

Fixtures come first: each case states the required outcome, and evaluators/resolver change only where one
of these demonstrated a gap. Thresholds are the registry's, never adjusted here.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

import analytics
from qs_evidence import ClaimTier, Dimension, Status, load_registry
from qs_evidence import assess as assess_mod
from qs_evidence.models import TIER_RANK, ClaimDecision, DimensionResult, EvidenceProfile
from qs_evidence.resolver import combine_decisions, resolve
from qs_evidence.sample import evaluate_anomaly, evaluate_correlation, evaluate_trend

START = date(2026, 5, 1)
REG = load_registry()


def pts(values, start=START):
    return [(start + timedelta(days=i), v) for i, v in enumerate(values) if v is not None]


def end_of(n, start=START):
    return start + timedelta(days=n - 1)


def rank(a: assess_mod.Assessment) -> int:
    return TIER_RANK[a.decision.tier]


def codes(a: assess_mod.Assessment) -> set[str]:
    return set(a.decision.limiting_factors)


def noisy(n, base=50.0):
    return [base + (i * 7) % 11 for i in range(n)]


# ---- anomaly: "no anomaly detected" vs "unable to detect reliably" -------------------------------


def _flat_with_one_extreme(n=40):
    values = [50.0] * n
    values[n // 2] = 95.0
    return values


def _mostly_flat_majority_tie(n=40):
    # >50% identical values => MAD is 0 although the series is not constant.
    values = [50.0] * 26 + [40.0, 41.0, 42.0, 43.0, 44.0, 60.0, 61.0, 62.0, 63.0, 64.0, 65.0, 66.0, 67.0, 68.0]
    return values[:n]


@pytest.mark.parametrize(
    "values", [_flat_with_one_extreme(), _mostly_flat_majority_tie()], ids=["one_extreme", "majority_tie"]
)
def test_zero_mad_series_is_unable_to_detect_not_uneventful(values):
    series = [analytics.Point(d, v) for d, v in pts(values)]
    # The numeric side is unchanged: the detector really does return nothing here ...
    assert analytics.detect_anomalies(series) == []
    # ... so the claim must say the detector could not score the series, not that nothing happened.
    a = assess_mod.assess_anomaly("m", pts(values), START, end_of(len(values)), effect={"n_anomalies": 0})
    assert a.decision.tier == ClaimTier.INSUFFICIENT
    assert "baseline_mad_zero" in codes(a)
    sample = a.profile.get(Dimension.SAMPLE)
    assert sample.status == Status.BLOCKING and sample.can_block


def test_constant_series_anomaly_is_still_blocked_by_zero_variance_only():
    a = assess_mod.assess_anomaly("m", pts([50.0] * 40), START, end_of(40))
    assert a.decision.tier == ClaimTier.INSUFFICIENT
    assert "baseline_zero_variance" in codes(a)
    assert "baseline_mad_zero" not in codes(a)  # existing reason codes are preserved, not duplicated


def test_anomaly_with_real_spread_is_not_blocked_by_the_mad_check():
    a = assess_mod.assess_anomaly("m", pts(noisy(40)), START, end_of(40))
    assert "baseline_mad_zero" not in codes(a)
    assert a.profile.get(Dimension.SAMPLE).status == Status.ADEQUATE


def test_anomaly_evaluator_reports_the_mad_check_with_observed_value():
    res = evaluate_anomaly([50.0] * 19 + [95.0] + [50.0] * 20)
    check = next(c for c in res.details["checks"] if c["name"] == "baseline_mad")
    assert check["observed"] == 0 and check["passed"] is False and check["can_block"] is True


# ---- trend: temporal spread, sparse observations, outliers --------------------------------------


def test_three_observations_on_one_day_do_not_support_a_trend():
    day = START
    a = assess_mod.assess_trend("m", [(day, 1.0), (day, 2.0), (day, 3.0)], START, day)
    assert a.decision.tier == ClaimTier.INSUFFICIENT
    assert {"n_below_minimum", "span_below_minimum"} <= codes(a)


def test_adequate_sample_count_on_a_single_day_is_blocked_by_span_alone():
    # 30 raw rows, one calendar day: the count passes, the temporal spread must not.
    res = evaluate_trend([START] * 30, [float(i) for i in range(30)])
    assert res.status == Status.BLOCKING
    assert res.reason_codes == ["span_below_minimum"]


def test_degenerate_time_axis_never_reports_a_direction():
    series = [analytics.Point(START, v) for v in (1.0, 5.0, 9.0)]
    out = analytics.calculate_trend(series)
    assert out["direction"] == "insufficient_data"
    assert out["slope_per_day"] is None and out["r_squared"] is None


def test_sparse_series_with_steep_slope_reports_the_data_limitation():
    values = [10.0 * i for i in range(8)]  # perfect, steep line, but 8 points
    a = assess_mod.assess_trend("m", pts(values), START, end_of(8), effect={"slope_per_day": 10.0})
    assert a.decision.tier == ClaimTier.INSUFFICIENT
    assert "n_below_minimum" in codes(a)


def test_adequate_count_but_clustered_in_time_cannot_be_supported():
    # 15 points, 21 days of span: the count and span thresholds pass, but the middle of the window is empty.
    values = [float(i) for i in range(7)] + [None] * 7 + [100.0 + i for i in range(8)]
    values = values + [None] * (22 - len(values))
    a = assess_mod.assess_trend("m", pts(values), START, end_of(22))
    assert a.profile.get(Dimension.SAMPLE).status == Status.ADEQUATE
    assert rank(a) < TIER_RANK[ClaimTier.SUPPORTED]
    assert {"long_gap", "uneven_coverage_across_window"} & codes(a)


def test_outlier_driven_trend_is_never_supported_while_robustness_is_unevaluated():
    values = noisy(30)
    values[-1] = 500.0  # one extreme final point drives the OLS slope
    a = assess_mod.assess_trend("m", pts(values), START, end_of(30))
    rob = a.profile.get(Dimension.ROBUSTNESS)
    assert rob.status == Status.NOT_ASSESSED and rob.reason_codes == [assess_mod.NOT_IMPLEMENTED]
    assert a.decision.tier != ClaimTier.SUPPORTED
    assert "robustness_not_assessed" in codes(a)


# ---- correlation: few pairs, constant series ----------------------------------------------------


def test_high_correlation_with_very_few_pairs_is_not_overstated():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [2.0, 4.0, 6.0, 8.0, 10.0]  # r == 1.0
    series_a = [analytics.Point(d, v) for d, v in pts(xs)]
    series_b = [analytics.Point(d, v) for d, v in pts(ys)]
    assert analytics.find_correlations(series_a, series_b)["r"] == 1.0
    a = assess_mod.assess_correlation("a", pts(xs), "b", pts(ys), START, end_of(5))
    assert a.decision.tier == ClaimTier.INSUFFICIENT
    assert "n_paired_below_minimum" in codes(a)


@pytest.mark.parametrize("constant_side", ["a", "b"])
def test_correlation_with_a_constant_series_is_unable_to_estimate(constant_side):
    varying, flat = noisy(30), [7.0] * 30
    xs, ys = (flat, varying) if constant_side == "a" else (varying, flat)
    series_a = [analytics.Point(d, v) for d, v in pts(xs)]
    series_b = [analytics.Point(d, v) for d, v in pts(ys)]
    assert analytics.find_correlations(series_a, series_b)["r"] is None
    a = assess_mod.assess_correlation("a", pts(xs), "b", pts(ys), START, end_of(30))
    assert a.decision.tier == ClaimTier.INSUFFICIENT
    assert "zero_variance" in codes(a)


def test_correlation_evaluator_zero_variance_ignores_unpaired_points():
    # b varies only on days a is missing: over the *pairs* b is constant.
    x = [1.0, 2.0, 3.0, None, None] * 8
    y = [5.0, 5.0, 5.0, 1.0, 9.0] * 8
    res = evaluate_correlation(x, y)
    assert "zero_variance" in res.reason_codes


def test_correlation_with_variance_is_not_blocked_by_the_zero_variance_check():
    res = evaluate_correlation(noisy(30), [float(i % 4) for i in range(30)])
    assert "zero_variance" not in res.reason_codes


# ---- missing evaluator / composites -------------------------------------------------------------


def test_registry_dimension_with_no_evaluator_is_never_adequate(monkeypatch):
    spec = REG["trend"]
    profile = EvidenceProfile(
        metric="m",
        analysis="trend",
        dimensions=[
            DimensionResult(dimension=d, status=Status.ADEQUATE)
            for d in Dimension
            if d in spec.dimensions and d != Dimension.ROBUSTNESS  # robustness: evaluator missing entirely
        ],
    )
    decision = resolve(profile, spec)
    assert decision.tier == ClaimTier.SUGGESTIVE
    assert "robustness_not_assessed" in decision.limiting_factors


@pytest.mark.parametrize("analysis", sorted(REG))
def test_every_dimension_without_an_evaluator_is_explicitly_not_assessed(analysis):
    spec = REG[analysis]
    a = assess_mod._finish(analysis, "m", None, {})  # no evaluator ran at all
    assert [d.dimension for d in a.profile.dimensions] == [d for d in Dimension if d in spec.dimensions]
    assert all(d.status == Status.NOT_ASSESSED for d in a.profile.dimensions)
    assert a.decision.tier in (ClaimTier.SUGGESTIVE, ClaimTier.SUPPORTED)  # tolerate-only specs may not cap
    for d in a.profile.dimensions:
        if spec.dimensions[d.dimension].on_not_assessed == "cap":
            assert f"{d.dimension.value}_not_assessed" in a.decision.limiting_factors
    assert a.decision.tier != ClaimTier.SUPPORTED or all(
        p.on_not_assessed == "tolerate" for p in spec.dimensions.values()
    )


def _decision(tier: ClaimTier, *factors: str) -> ClaimDecision:
    return ClaimDecision(tier=tier, limiting_factors=list(factors), permitted_phrasing_class=tier.value)


@pytest.mark.parametrize("weak_tier", [t for t in ClaimTier if t != ClaimTier.SUPPORTED])
@pytest.mark.parametrize("position", [0, 1, 2])
def test_composite_never_exceeds_its_weakest_component(weak_tier, position):
    parts = [_decision(ClaimTier.SUPPORTED), _decision(ClaimTier.SUPPORTED), _decision(ClaimTier.SUPPORTED)]
    parts[position] = _decision(weak_tier, "why")
    out = combine_decisions(parts)
    assert out.tier == weak_tier and "why" in out.limiting_factors
