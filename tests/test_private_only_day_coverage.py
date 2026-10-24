"""A day holding only private-metric data must not count as a day with data.

read_health_data pivots every metric column, private ones included, and only
redacts values afterwards. A day on which just a HEALTH_PRIVATE_FIELDS metric
was logged therefore still comes back as an all-null row, and it is counted in
summary.days_with_data and coverage (days_with_data, missing_days,
coverage_percent). That exposes the dates the private metric was logged on,
and reports coverage that no public metric backs.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_read_health_data_ignores_days_with_only_private_data(tmp_path, monkeypatch):
    monkeypatch.setenv("HEALTH_DB_PATH", str(tmp_path / "health.db"))
    monkeypatch.setenv("HEALTH_PRIVATE_FIELDS", "weight_kg")
    for name in ("server", "privacy", "tools.health", "tools.measurements", "tools.workouts"):
        sys.modules.pop(name, None)
    import server

    # Two days with a public metric, three days with only the private one.
    for day in ("2026-01-01", "2026-01-02"):
        server.log_daily_metric(date=day, steps=1000)
    for day in ("2026-01-03", "2026-01-04", "2026-01-05"):
        server.log_daily_metric(date=day, weight_kg=80.0)

    result = server.read_health_data(start_date="2026-01-01", end_date="2026-01-05")

    observed = {
        "row_dates": [row.date for row in result.rows],
        "summary_days_with_data": result.summary.days_with_data,
        "coverage_days_with_data": result.coverage.days_with_data,
        "coverage_missing_days": result.coverage.missing_days,
        "coverage_percent": result.coverage.coverage_percent,
    }
    assert observed == {
        "row_dates": ["2026-01-01", "2026-01-02"],
        "summary_days_with_data": 2,
        "coverage_days_with_data": 2,
        "coverage_missing_days": 3,
        "coverage_percent": 40.0,
    }
