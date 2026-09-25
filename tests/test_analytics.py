"""
Unit tests for analytics.py — pure functions, no database or MCP involved
(mirrors the style of test_logic.py).
"""

import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import analytics as a


def series(values, start=date(2026, 1, 1)):
    return [a.Point(start + timedelta(days=i), v) for i, v in enumerate(values)]


def series_on_days(day_offsets, values, start=date(2026, 1, 1)):
    """Build a series at arbitrary (non-consecutive) calendar offsets from
    `start`, for exercising gaps/sparse spacing — as opposed to `series()`,
    which always lays points on consecutive days.
    """
    assert len(day_offsets) == len(values)
    return [a.Point(start + timedelta(days=d), v) for d, v in zip(day_offsets, values, strict=True)]


def reference_slope(day_offsets, values):
    """Ground truth: OLS slope against real calendar-day offsets, computed
    independently via the standard library rather than anything in
    analytics.py. This is what a caller would get by literally plotting
    the points against their real calendar dates — the number
    calculate_trend must match regardless of how irregular the spacing is.
    """
    slope, _intercept = statistics.linear_regression(day_offsets, values)
    return slope


def test_baseline_of_empty_series_is_all_none_with_n_zero():
    result = a.baseline([])
    assert result == {"mean": None, "median": None, "stdev": None, "n": 0}


def test_baseline_computes_mean_median_stdev():
    result = a.baseline(series([1, 2, 3, 4, 5]))
    assert result["mean"] == 3.0
    assert result["median"] == 3.0
    assert result["n"] == 5


def test_detect_anomalies_requires_at_least_five_points():
    assert a.detect_anomalies(series([1, 2, 3, 100])) == []


def test_detect_anomalies_flags_a_clear_outlier():
    result = a.detect_anomalies(series([7, 7.2, 6.8, 7.1, 6.9, 7.0, 2.0]))
    assert len(result) == 1
    assert result[0]["date"] == "2026-01-07"
    assert result[0]["direction"] == "below"


def test_detect_anomalies_on_a_flat_series_finds_nothing():
    # Zero spread (MAD == 0) must not raise a ZeroDivisionError.
    assert a.detect_anomalies(series([5, 5, 5, 5, 5, 5])) == []


def test_calculate_trend_needs_at_least_three_points():
    result = a.calculate_trend(series([1, 2]))
    assert result["direction"] == "insufficient_data"
    assert result["span_days"] is None


def test_calculate_trend_detects_increasing_series():
    result = a.calculate_trend(series([1, 2, 3, 4, 5]))
    assert result["direction"] == "increasing"
    assert result["slope_per_day"] == 1.0
    assert result["r_squared"] == 1.0
    assert result["span_days"] == 4


def test_calculate_trend_on_flat_series():
    result = a.calculate_trend(series([5, 5, 5, 5]))
    assert result["direction"] == "flat"
    assert result["slope_per_day"] == 0.0


# ---------------------------------------------------------------------------
# Gap handling: calculate_trend must regress against real calendar time,
# not position in the list. Every test below builds a series with irregular
# spacing and checks the reported slope against an independently computed
# reference regression over the real day offsets (see reference_slope()),
# so a regression back to xs = range(n) would be caught even though it
# "looks like" a valid trend.
# ---------------------------------------------------------------------------


def test_calculate_trend_continuous_30_days():
    day_offsets = list(range(30))
    values = [60 + 0.5 * d for d in day_offsets]
    result = a.calculate_trend(series_on_days(day_offsets, values))
    assert result["n"] == 30
    assert result["span_days"] == 29
    assert result["slope_per_day"] == round(reference_slope(day_offsets, values), 4)
    assert result["r_squared"] > 0.99


def test_calculate_trend_with_missing_middle_days():
    # 30 calendar days of an underlying linear trend, but the middle third
    # (days 10-19) never got logged — a classic "missed a week and a half"
    # gap. Position-based fitting (xs = range(n)) would compress that
    # 10-day hole into a single step and report a steeper slope than the
    # data supports.
    day_offsets = [d for d in range(30) if not (10 <= d <= 19)]
    values = [60 + 0.5 * d for d in day_offsets]
    result = a.calculate_trend(series_on_days(day_offsets, values))
    assert result["n"] == 20
    assert result["span_days"] == 29
    assert result["slope_per_day"] == round(reference_slope(day_offsets, values), 4)
    # The true per-day slope is 0.5 — this is what a position-based fit
    # would get wrong (it would report ~0.5 * 29/19 ≈ 0.76 instead).
    assert result["slope_per_day"] == 0.5


def test_calculate_trend_sparse_observations():
    # Only 5 points scattered unevenly across ~40 days.
    day_offsets = [0, 5, 12, 27, 41]
    values = [60, 61, 63.5, 70.5, 80.5]
    result = a.calculate_trend(series_on_days(day_offsets, values))
    assert result["n"] == 5
    assert result["span_days"] == 41
    assert result["slope_per_day"] == round(reference_slope(day_offsets, values), 4)


def test_calculate_trend_single_long_gap():
    # The exact shape from the bug report: a couple of points at the start
    # of the month, then nothing until a single reading near the end.
    day_offsets = [0, 1, 19]  # Jan 1, Jan 2, Jan 20
    values = [60, 61, 80]
    result = a.calculate_trend(series_on_days(day_offsets, values))
    assert result["n"] == 3
    assert result["span_days"] == 19
    naive_slope = (values[-1] - values[0]) / (len(values) - 1)  # what xs=range(n) would imply
    assert result["slope_per_day"] == round(reference_slope(day_offsets, values), 4)
    assert result["slope_per_day"] != round(naive_slope, 4)


def test_calculate_trend_observations_only_at_beginning_and_end():
    # Three points: two close together right at the start, one far away
    # at the end, nothing in between — an extreme, minimal-n gap case.
    day_offsets = [0, 2, 60]
    values = [60, 60.4, 84]
    result = a.calculate_trend(series_on_days(day_offsets, values))
    assert result["n"] == 3
    assert result["span_days"] == 60
    assert result["slope_per_day"] == round(reference_slope(day_offsets, values), 4)


def test_compare_periods_computes_delta_and_pct_change():
    result = a.compare_periods(series([9, 11], start=date(2026, 2, 1)), series([4, 6], start=date(2026, 1, 1)))
    assert result["delta"] == 5.0
    assert result["pct_change"] == 100.0


def test_compare_periods_with_an_empty_period_returns_nones():
    result = a.compare_periods([], series([1, 2, 3]))
    assert result["delta"] is None
    assert result["pct_change"] is None


def test_find_correlations_needs_at_least_four_overlapping_days():
    result = a.find_correlations(series([1, 2, 3]), series([1, 2, 3]))
    assert result["r"] is None
    assert result["n"] == 3


def test_find_correlations_detects_perfect_positive_correlation():
    result = a.find_correlations(series([1, 2, 3, 4, 5]), series([2, 4, 6, 8, 10]))
    assert result["r"] == 1.0
    assert result["n"] == 5


def test_find_correlations_with_lag_shifts_the_second_series():
    # series_b repeats series_a's exact (non-monotonic) pattern one day
    # later, so lag_days=1 should recover a perfect correlation, while
    # lag_days=0 joins the two series on the wrong days entirely.
    series_a = series([1, 5, 2, 8], start=date(2026, 1, 1))
    series_b = series([1, 5, 2, 8], start=date(2026, 1, 2))
    lagged = a.find_correlations(series_a, series_b, lag_days=1)
    unlagged = a.find_correlations(series_a, series_b, lag_days=0)
    assert lagged["r"] == 1.0
    assert unlagged["r"] != 1.0


def test_find_correlations_no_sample_confidence_field():
    """find_correlations must stay silent about how much to trust the result — no bucketed read on
    "n" alone. That judgment belongs to qs_evidence.assess_correlation's sample dimension, which folds
    the paired count into a single claim decision instead of a second, competing confidence label."""
    insufficient = a.find_correlations(series([1, 2, 3]), series([1, 2, 3]))
    zero_variance = a.find_correlations(series([1, 1, 1, 1]), series([1, 2, 3, 4]))
    success = a.find_correlations(series([1, 2, 3, 4, 5]), series([2, 4, 6, 8, 10]))
    for result in (insufficient, zero_variance, success):
        assert "sample_confidence" not in result
    assert insufficient["r"] is None
    assert zero_variance["r"] is None and zero_variance["n"] == 4
    assert success["r"] == 1.0
