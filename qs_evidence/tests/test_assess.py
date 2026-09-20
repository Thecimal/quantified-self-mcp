from datetime import date, timedelta

import pytest

from qs_evidence import ClaimTier, Dimension, Status, load_registry
from qs_evidence import assess as assess_mod
from qs_evidence.models import TIER_RANK

START = date(2026, 5, 1)
REG = load_registry()


def pts(values, start=START):
    """(day, value) pairs; None values are skipped, like a day with no measurement."""
    return [(start + timedelta(days=i), v) for i, v in enumerate(values) if v is not None]


def wave(n, base=50.0):
    return [base + (i % 5) for i in range(n)]


def end_of(n, start=START):
    return start + timedelta(days=n - 1)


def gap_last(values, k):
    return values[: len(values) - k] + [None] * k


DATA_FACTORS = {"robustness_not_assessed", "practical_not_assessed", "provenance_not_assessed"}


def test_align_places_values_by_day_and_drops_non_finite():
    out = assess_mod.align(
        [(START, 1.0), (START + timedelta(days=2), float("nan")), (START + timedelta(days=9), 5)],
        START,
        START + timedelta(days=3),
    )
    assert out == [1.0, None, None, None]


@pytest.mark.parametrize("analysis", sorted(REG))
def test_every_registry_analysis_has_an_assess_entry_point_and_lists_exactly_its_dimensions(analysis):
    v = wave(60)
    calls = {
        "trend": lambda: assess_mod.assess_trend("m", pts(v), START, end_of(60)),
        "anomaly": lambda: assess_mod.assess_anomaly("m", pts(v), START, end_of(60)),
        "correlation": lambda: assess_mod.assess_correlation("a", pts(v), "b", pts(v), START, end_of(60)),
        "window_comparison": lambda: assess_mod.assess_window_comparison(
            "m",
            pts(v[30:], START + timedelta(days=30)),
            (START + timedelta(days=30), end_of(60)),
            pts(v[:30]),
            (START, end_of(30)),
        ),
    }
    a = calls[analysis]()
    assert {d.dimension for d in a.profile.dimensions} == set(REG[analysis].dimensions)
    assert a.profile.analysis == analysis


def test_unbuilt_dimensions_are_explicit_not_assessed_never_silently_adequate():
    a = assess_mod.assess_trend("m", pts(wave(30)), START, end_of(30))
    by = {d.dimension: d for d in a.profile.dimensions}
    for dim in (Dimension.ROBUSTNESS, Dimension.PRACTICAL, Dimension.PROVENANCE, Dimension.MEASUREMENT_VALIDITY):
        assert by[dim].status == Status.NOT_ASSESSED and by[dim].reason_codes == [assess_mod.NOT_IMPLEMENTED]
    for dim in (Dimension.SAMPLE, Dimension.TEMPORAL, Dimension.MISSINGNESS):
        assert by[dim].status == Status.ADEQUATE


# ---- same statistical effect, different evidence -> different decision ----------------------------


def test_trend_clean_vs_recent_gap_vs_tiny_sample():
    effect = {"slope_per_day": -0.5}
    clean = assess_mod.assess_trend("m", pts(wave(30)), START, end_of(30), effect)
    gappy = assess_mod.assess_trend("m", pts(gap_last(wave(30), 6)), START, end_of(30), effect)
    tiny = assess_mod.assess_trend("m", pts(wave(10)), START, end_of(10), effect)

    assert clean.profile.effect == gappy.profile.effect == effect
    assert set(clean.decision.limiting_factors) == DATA_FACTORS  # nothing wrong with the data itself
    assert "recent_window_gap" in gappy.decision.limiting_factors
    assert "recent_window_gap" not in clean.decision.limiting_factors
    assert tiny.decision.tier == ClaimTier.INSUFFICIENT
    assert "n_below_minimum" in tiny.decision.limiting_factors
    # weakest-link: worse evidence never raises the tier
    ranks = [TIER_RANK[x.decision.tier] for x in (clean, gappy, tiny)]
    assert ranks == sorted(ranks, reverse=True)


def test_trend_span_too_short_is_insufficient_even_with_enough_points():
    a = assess_mod.assess_trend("m", pts(wave(15)), START, end_of(15))  # n=15 >= 14 but span=14 < 21
    assert a.decision.tier == ClaimTier.INSUFFICIENT and "span_below_minimum" in a.decision.limiting_factors


def test_window_comparison_degradation():
    a_start, b_start = START + timedelta(days=14), START
    b = pts([50.0 + (i % 3) for i in range(14)], b_start)
    rng_a, rng_b = (a_start, end_of(14, a_start)), (b_start, end_of(14, b_start))
    good_a = [60.0 + (i % 3) for i in range(14)]
    clean = assess_mod.assess_window_comparison("m", pts(good_a, a_start), rng_a, b, rng_b, {"pct_change": 20})
    gappy = assess_mod.assess_window_comparison(
        "m", pts(gap_last(good_a, 6), a_start), rng_a, b, rng_b, {"pct_change": 20}
    )
    tiny = assess_mod.assess_window_comparison("m", pts(good_a[:5], a_start), rng_a, b, rng_b, {"pct_change": 20})
    assert "recent_window_gap" not in clean.decision.limiting_factors
    assert "recent_window_gap" in gappy.decision.limiting_factors
    assert tiny.decision.tier == ClaimTier.INSUFFICIENT and "n_below_minimum" in tiny.decision.limiting_factors
    temporal = {d.dimension: d for d in gappy.profile.dimensions}[Dimension.TEMPORAL]
    assert temporal.details["windows"]["period_a"]["status"] == "weak"  # which window is preserved in details
    assert temporal.details["windows"]["period_b"]["status"] == "adequate"


def test_window_comparison_uses_registry_per_window_coverage_threshold():
    a_start = START + timedelta(days=14)
    b = pts(wave(14), START)
    sparse_a = [50.0 if i % 2 == 0 else None for i in range(14)]  # 7 obs (== min n) but 50% coverage < 0.6
    a = assess_mod.assess_window_comparison(
        "m", pts(sparse_a, a_start), (a_start, end_of(14, a_start)), b, (START, end_of(14))
    )
    assert "coverage_below_threshold" in a.decision.limiting_factors


def test_anomaly_short_baseline_insufficient_and_gap_only_limits():
    effect = {"n_anomalies": 1}
    ok = assess_mod.assess_anomaly("m", pts(wave(60)), START, end_of(60), effect)
    short = assess_mod.assess_anomaly("m", pts(wave(20)), START, end_of(20), effect)
    gappy = assess_mod.assess_anomaly("m", pts(gap_last(wave(60), 8)), START, end_of(60), effect)
    assert ok.decision.tier != ClaimTier.INSUFFICIENT
    assert short.decision.tier == ClaimTier.INSUFFICIENT and "baseline_too_short" in short.decision.limiting_factors
    assert "recent_window_gap" in gappy.decision.limiting_factors


# ---- correlation: n_paired blocks, n_eff never does ----------------------------------------------


def naive_pairs(a, b, lag):
    bmap = {d: v for d, v in b}
    return [(v, bmap[d + timedelta(days=lag)]) for d, v in a if d + timedelta(days=lag) in bmap]


@pytest.mark.parametrize("lag", [0, 1, 3, -2])
def test_correlation_pairing_matches_a_naive_date_join(lag):
    a = pts([None if i % 7 == 3 else 50.0 + i % 6 for i in range(45)])
    b = pts([None if i % 5 == 1 else 20.0 + (i * 7) % 11 for i in range(45)])
    x = assess_mod.align(a, START, end_of(45))
    y = assess_mod.align(b, START + timedelta(days=lag), end_of(45) + timedelta(days=lag))
    n = sum(p is not None and q is not None for p, q in zip(x, y, strict=True))
    assert n == len(naive_pairs(a, b, lag))


def test_correlation_small_n_paired_is_insufficient():
    v = wave(10)
    a = assess_mod.assess_correlation("a", pts(v), "b", pts(v[::-1]), START, end_of(10))
    assert a.decision.tier == ClaimTier.INSUFFICIENT and "n_paired_below_minimum" in a.decision.limiting_factors


def test_low_n_eff_alone_never_makes_a_correlation_insufficient():
    n = 40
    slow_a = [100.0 + i * 3 + (i % 2) for i in range(n)]  # near-linear ramps: lag-1 autocorrelation ~ 1
    slow_b = [10.0 + i * 0.5 + (i % 3) * 0.1 for i in range(n)]
    n_eff = assess_mod.lag1_n_eff(
        assess_mod.align(pts(slow_a), START, end_of(n)), assess_mod.align(pts(slow_b), START, end_of(n)), n
    )
    assert n_eff is not None and n_eff < 10  # the diagnostic really is tripped
    a = assess_mod.assess_correlation("a", pts(slow_a), "b", pts(slow_b), START, end_of(n))
    sample = {d.dimension: d for d in a.profile.dimensions}[Dimension.SAMPLE]
    assert sample.status == Status.WEAK and not sample.can_block and "n_eff_low" in sample.reason_codes
    assert a.decision.tier != ClaimTier.INSUFFICIENT and "n_eff_low" in a.decision.limiting_factors


def test_n_eff_is_bounded_by_n_paired_and_handles_constant_series():
    x = assess_mod.align(pts(wave(30)), START, end_of(30))
    flat = assess_mod.align(pts([5.0] * 30), START, end_of(30))
    assert assess_mod.lag1_n_eff(x, flat, 30) is None
    alt = assess_mod.align(pts([1.0, -1.0] * 15), START, end_of(30))  # rx*ry -> +1 with itself, negative with shift
    assert assess_mod.lag1_n_eff(alt, alt, 30) <= 30
