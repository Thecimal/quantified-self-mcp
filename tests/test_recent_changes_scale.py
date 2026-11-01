"""
get_recent_changes on a multi-year database: how often it sets up and queries the database.

get_recent_changes used to read each metric's recent and baseline windows with its own _fetch_metric_series call:
18 reads for 9 metrics. Every read first ran _ensure_db, the schema and trigger bootstrap, so on three years of daily
data about 80% of a call was that setup (about 120 of 153 ms), while the 18 queries took about 14 ms. It now reads every
metric over both windows once.

Wall-clock time is not asserted (it depends on the machine). As test_import_purge_scope.py does for the import purge,
these pin what the speed-up rests on: the number of setups and queries per call, the query plan, and a VM-step bound
that does not depend on the clock. Run with -s to see the timings printed. The data is synthetic and deterministic:
1,095 days, nine metrics, with gaps, ending today.
"""

import math
import sqlite3
import statistics
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastmcp import Client

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.agent.runner import loaded_server  # noqa: E402
from logic import connect_writable, daily_metrics_wide, ensure_schema, import_measurements  # noqa: E402

DAYS = 1095  # three years
METRICS = [
    "steps",
    "sleep_hours",
    "resting_heart_rate",
    "hrv_ms",
    "weight_kg",
    "workout_minutes",
    "mood",
    "water_ml",
    "heart_rate",
]
WINDOW = 14  # get_recent_changes(days=14): a 14-day recent window and the 56 days before it


def _value(metric, i):
    wave = math.sin(i / 11.0)
    return {
        "steps": 8000 + 2500 * wave + (i % 7) * 120,
        "sleep_hours": 7.2 + 0.8 * math.sin(i / 5.0),
        "resting_heart_rate": 58 + 4 * math.sin(i / 23.0) + (i % 5),
        "hrv_ms": 55 + 9 * math.sin(i / 17.0),
        "weight_kg": 78 + 2 * math.sin(i / 60.0),
        "workout_minutes": 20 + 25 * (1 + math.sin(i / 3.0)) / 2,
        "mood": 5 + 2 * math.sin(i / 9.0),
        "water_ml": 1800 + 400 * math.sin(i / 4.0),
        "heart_rate": 72 + 6 * math.sin(i / 13.0),
    }[metric]


@pytest.fixture(scope="module")
def three_year_db(tmp_path_factory):
    today = date.today()
    rows = []
    for i in range(DAYS):
        day = (today - timedelta(days=i)).isoformat()
        for metric in METRICS:
            if metric == "steps" and 3 <= i % 11 <= 5:
                continue  # a gap, so some days have no value for a metric that others do
            if metric == "mood" and i % 2:
                continue
            rows.append(
                {
                    "timestamp": f"{day}T08:00:00",
                    "metric": metric,
                    "value": round(_value(metric, i)),
                    "source": "Watch",
                    "source_type": "wearable",
                }
            )
    path = tmp_path_factory.mktemp("scale") / "health.db"
    conn = connect_writable(path)
    try:
        ensure_schema(conn)
        import_measurements(conn, "synthetic", rows)
    finally:
        conn.close()
    return path


async def test_get_recent_changes_sets_up_and_queries_the_database_a_constant_number_of_times(
    three_year_db, monkeypatch
):
    calls = {"ensure_db": 0, "daily_metrics_wide": 0}
    with loaded_server(three_year_db) as server:
        ensure_db, wide = server._ensure_db, server.daily_metrics_wide

        def counting_ensure_db(path):
            calls["ensure_db"] += 1
            return ensure_db(path)

        def counting_wide(*args, **kwargs):
            calls["daily_metrics_wide"] += 1
            return wide(*args, **kwargs)

        async with Client(server.mcp) as client:
            result = (await client.call_tool("get_recent_changes", {"days": WINDOW})).structured_content
            timings = []
            for _ in range(5):
                start = time.perf_counter()
                await client.call_tool("get_recent_changes", {"days": WINDOW})
                timings.append((time.perf_counter() - start) * 1000)
            monkeypatch.setattr(server, "_ensure_db", counting_ensure_db)
            monkeypatch.setattr(server, "daily_metrics_wide", counting_wide)
            await client.call_tool("get_recent_changes", {"days": WINDOW})

    median_ms = statistics.median(timings)
    print(f"\nget_recent_changes(days={WINDOW}) on {DAYS} days x {len(METRICS)} metrics: median {median_ms:.1f} ms")
    assert result["changes"], "the synthetic data should give the tool something to report; the test proved nothing"
    # One query for every metric over both windows. It was one per metric and window: 18.
    assert calls["daily_metrics_wide"] == 1
    # One setup for that read, plus the data-health read at the end. It was 19: one before every query.
    assert calls["ensure_db"] <= 2


def test_the_combined_read_is_an_index_range_scan_over_the_window_not_the_whole_table(three_year_db):
    end = date.today()
    recent_start = end - timedelta(days=WINDOW)
    baseline_start = recent_start - timedelta(days=WINDOW * 4)
    conn = sqlite3.connect(three_year_db)
    try:
        steps = [0]
        conn.set_progress_handler(lambda: steps.__setitem__(0, steps[0] + 1) or 0, 1)
        rows = daily_metrics_wide(conn, METRICS, baseline_start.isoformat(), end.isoformat())
        conn.set_progress_handler(None, 0)
        plan = " ".join(
            row[3]
            for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT date, MAX(CASE WHEN metric = ? THEN value END) FROM daily_metrics "
                "WHERE metric IN (?) AND date >= ? AND date <= ? GROUP BY date ORDER BY date",
                ("steps", "steps", baseline_start.isoformat(), end.isoformat()),
            )
        )
    finally:
        conn.close()
    assert len(rows) == WINDOW * 5 + 1  # every day of both windows has at least one metric
    assert "SEARCH daily_metrics USING INDEX" in plan and "SCAN daily_metrics" not in plan
    # About 41,000 VM steps measured for 9 metrics over 70 days. A scan of the whole 3-year table costs about 78,000+.
    assert steps[0] < 60_000, f"{steps[0]} VM steps: the read no longer stays inside the window"


async def test_one_read_for_all_metrics_matches_one_read_per_metric_and_per_window(three_year_db):
    end = date.today()
    recent_start = end - timedelta(days=WINDOW)
    baseline_start = recent_start - timedelta(days=WINDOW * 4)
    baseline_end = recent_start - timedelta(days=1)
    with loaded_server(three_year_db) as server:
        combined = server._fetch_metrics_series(METRICS, baseline_start, end)
        assert list(combined) == METRICS
        for metric in METRICS:
            whole = server._fetch_metric_series(metric, baseline_start, end)
            assert combined[metric] == whole
            assert [p for p in combined[metric] if p.day >= recent_start] == server._fetch_metric_series(
                metric, recent_start, end
            )
            assert [p for p in combined[metric] if p.day <= baseline_end] == server._fetch_metric_series(
                metric, baseline_start, baseline_end
            )
        # The gaps are real: some days are missing for steps and mood, so the split is not trivially complete.
        assert 0 < len(combined["steps"]) < WINDOW * 5 + 1
        assert 0 < len(combined["mood"]) < len(combined["sleep_hours"])
        assert server._fetch_metrics_series([], baseline_start, end) == {}
