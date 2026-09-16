from datetime import date

from analytics import Point
from evidence import build_coverage_summary, build_evidence


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


def test_moderate_coverage_with_small_recent_gap_is_moderate():
    # 70% coverage (14/20 days), and the only gap is 3 days but doesn't
    # touch the end of the range -> recent_gap_days stays 0, so this
    # should land in "moderate" (>=0.6 coverage, <=7 recent gap), not
    # "high" (needs >=0.85) or "low".
    start, end = date(2026, 1, 1), date(2026, 1, 20)
    present_days = [d for d in range(1, 21) if d not in (5, 6, 7, 15, 16, 17)]
    series = _series([date(2026, 1, d) for d in present_days])
    ev = build_evidence(series, start, end)
    assert ev["observed_days"] == 14
    assert ev["coverage_ratio"] == 0.7
    assert ev["recent_gap_days"] == 0
    assert ev["confidence"] == "moderate"


def test_coverage_summary_complete_data():
    start, end = date(2026, 1, 1), date(2026, 1, 5)
    rows = [{"sleep_hours": 7.5, "steps": 8000} for _ in range(5)]
    summary = build_coverage_summary(rows, start, end, ["sleep_hours", "steps"])
    assert summary["days_expected"] == 5
    assert summary["days_with_data"] == 5
    assert summary["coverage_percent"] == 100.0
    assert summary["missing_days"] == 0
    assert summary["metrics"] == {"sleep_hours": 100.0, "steps": 100.0}
    assert summary["confidence"] == "high"
    assert summary["period"] == "2026-01-01/2026-01-05"


def test_coverage_summary_partial_and_uneven_per_metric():
    start, end = date(2026, 1, 1), date(2026, 1, 10)
    # 10 days present at all (so overall coverage_percent is 100%), but
    # hrv_ms is only actually logged on 3 of them — the per-metric split
    # is the whole point, since a high day-level count can still hide a
    # near-empty individual metric.
    rows = [{"sleep_hours": 7.0, "hrv_ms": 55.0 if i < 3 else None} for i in range(10)]
    summary = build_coverage_summary(rows, start, end, ["sleep_hours", "hrv_ms"])
    assert summary["days_with_data"] == 10
    assert summary["coverage_percent"] == 100.0
    assert summary["metrics"] == {"sleep_hours": 100.0, "hrv_ms": 30.0}
    # basis for confidence is the average across metrics (100 + 30) / 2 = 65 -> moderate
    assert summary["confidence"] == "moderate"


def test_coverage_summary_completely_missing_metric():
    start, end = date(2026, 1, 1), date(2026, 1, 5)
    rows = [{"sleep_hours": 7.0} for _ in range(5)]
    summary = build_coverage_summary(rows, start, end, ["sleep_hours", "hrv_ms"])
    assert summary["metrics"]["hrv_ms"] == 0.0
    assert summary["confidence"] == "low"  # (100 + 0) / 2 = 50 -> low


def test_coverage_summary_no_days_at_all():
    start, end = date(2026, 1, 1), date(2026, 1, 5)
    summary = build_coverage_summary([], start, end, ["sleep_hours"])
    assert summary["days_with_data"] == 0
    assert summary["coverage_percent"] == 0.0
    assert summary["missing_days"] == 5
    assert summary["metrics"] == {"sleep_hours": 0.0}
    assert summary["confidence"] == "low"
