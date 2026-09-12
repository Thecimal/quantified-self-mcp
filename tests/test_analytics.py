"""
Unit tests for analytics.py — pure functions, no database or MCP involved
(mirrors the style of test_logic.py).
"""

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import analytics as a


def series(values, start=date(2026, 1, 1)):
    return [a.Point(start + timedelta(days=i), v) for i, v in enumerate(values)]


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


def test_calculate_trend_detects_increasing_series():
    result = a.calculate_trend(series([1, 2, 3, 4, 5]))
    assert result["direction"] == "increasing"
    assert result["slope_per_day"] == 1.0
    assert result["r_squared"] == 1.0


def test_calculate_trend_on_flat_series():
    result = a.calculate_trend(series([5, 5, 5, 5]))
    assert result["direction"] == "flat"
    assert result["slope_per_day"] == 0.0


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
