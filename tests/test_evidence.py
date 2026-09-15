from datetime import date

from analytics import Point
from evidence import build_evidence


def _series(days):
    return [Point(d, 1.0) for d in days]


def test_full_coverage_is_high_confidence():
    start, end = date(2026, 1, 1), date(2026, 1, 10)
    series = _series([date(2026, 1, d) for d in range(1, 11)])
    ev = build_evidence(series, start, end)
    assert ev["expected_days"] == 10
    assert ev["observed_days"] == 10
    assert ev["coverage_ratio"] == 1.0
    assert ev["missing_days"] == 0
    assert ev["gaps"] == []
    assert ev["confidence"] == "high"


def test_partial_coverage_with_recent_gap_is_low():
    start, end = date(2026, 1, 1), date(2026, 1, 20)
    series = _series([date(2026, 1, d) for d in range(1, 9)])
    ev = build_evidence(series, start, end)
    assert ev["observed_days"] == 8
    assert ev["missing_days"] == 12
    assert len(ev["gaps"]) == 1
    assert ev["gaps"][0]["days"] == 12
    assert ev["recent_gap_days"] == 12
    assert ev["confidence"] == "low"


def test_no_data_at_all():
    start, end = date(2026, 1, 1), date(2026, 1, 5)
    ev = build_evidence([], start, end)
    assert ev["observed_days"] == 0
    assert ev["coverage_ratio"] == 0.0
    assert ev["observed_start"] is None
    assert ev["observed_end"] is None
    assert ev["freshness_days"] is None
    assert ev["confidence"] == "low"


def test_middle_gap_is_not_a_recent_gap():
    start, end = date(2026, 1, 1), date(2026, 1, 10)
    series = _series([date(2026, 1, d) for d in [1, 2, 3, 7, 8, 9, 10]])
    ev = build_evidence(series, start, end)
    assert len(ev["gaps"]) == 1
    assert ev["gaps"][0]["days"] == 3
    assert ev["recent_gap_days"] == 0
    assert ev["freshness_days"] == 0


def test_single_day_range():
    start = end = date(2026, 1, 1)
    ev = build_evidence(_series([date(2026, 1, 1)]), start, end)
    assert ev["expected_days"] == 1
    assert ev["coverage_ratio"] == 1.0
