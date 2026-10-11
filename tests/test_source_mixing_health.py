"""
Analytics over a window that mixes sources must say so.

daily_metrics keeps one source per (metric, day), but the analytics tools read it as one anonymous series.
When the winning source changes inside a window (a device switch), differs between two compared windows, or
was picked with no configured priority (resolution 'fallback'), the number is partly a statement about the
devices, not the person. These tests drive the real MCP tools and require data_health to report it, while
an explicit source_priority (a documented rule) and a single source stay VALID.
"""

import sys
from datetime import date, timedelta
from pathlib import Path

from fastmcp import Client

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from data_health import SEVERITY_ORDER, compose_data_health  # noqa: E402
from db import invariant  # noqa: E402
from eval.agent.runner import loaded_server  # noqa: E402
from logic import connect_writable, ensure_schema, import_measurements  # noqa: E402

TODAY = date.today()
GARMIN, APPLE = "Garmin", "Apple Watch"


def _iso(days_ago):
    return (TODAY - timedelta(days=days_ago)).isoformat()


def _hrv(days_ago, value, source, hour=7):
    return {
        "timestamp": f"{_iso(days_ago)}T{hour:02d}:00:00",
        "metric": "hrv_ms",
        "value": value,
        "unit": "ms",
        "source": source,
    }


def _sleep(days_ago):
    return {
        "timestamp": f"{_iso(days_ago)}T12:00:00",
        "metric": "sleep_hours",
        "value": 7 + (days_ago % 4) * 0.1,
        "unit": "h",
        "source": "Apple Watch",
    }


def _build(tmp_path, garmin_days=(), apple_days=(), priority=None, with_sleep=False):
    db = tmp_path / "health.db"
    conn = connect_writable(db)
    ensure_schema(conn)
    if garmin_days:
        import_measurements(conn, "garmin", [_hrv(n, 80 + n % 3, GARMIN, hour=6 + n % 2) for n in garmin_days])
    if apple_days:
        import_measurements(conn, "apple-health", [_hrv(n, 50 + n % 3, APPLE, hour=8) for n in apple_days])
    if with_sleep:
        import_measurements(conn, "apple-health", [_sleep(n) for n in range(29, -1, -1)])
    if priority:
        invariant.set_source_priority(conn, "hrv_ms", priority)
    conn.close()
    return db


async def _call(db, tool, arguments):
    with loaded_server(db) as server:
        async with Client(server.mcp) as client:
            result = await client.call_tool(tool, arguments)
    return result.structured_content


WINDOW = {"start_date": _iso(29), "end_date": _iso(0)}
BOTH = range(29, -1, -1)


async def test_a_device_switch_between_compared_windows_is_reported_not_presented_as_a_change(tmp_path):
    db = _build(tmp_path, garmin_days=range(59, 29, -1), apple_days=range(29, -1, -1))
    result = await _call(
        db,
        "compare_metric_periods",
        {
            "metric": "hrv_ms",
            "period_a_start": _iso(29),
            "period_a_end": _iso(0),
            "period_b_start": _iso(59),
            "period_b_end": _iso(30),
        },
    )
    assert result["delta"] == -30.0  # the arithmetic is unchanged; only its standing is
    assert result["data_health"]["status"] == "MIXED_SOURCES"
    assert "source_changed_between_windows" in result["data_health"]["reasons"]


async def test_disagreeing_sources_with_no_priority_are_reported_on_a_baseline(tmp_path):
    db = _build(tmp_path, garmin_days=BOTH, apple_days=BOTH)
    result = await _call(db, "get_baseline", {"metric": "hrv_ms", **WINDOW})
    assert result["data_health"]["status"] == "MIXED_SOURCES"
    assert result["data_health"]["reasons"] == ["unranked_source_conflict_in_window"]


async def test_the_same_report_reaches_history_anomalies_and_trend(tmp_path):
    db = _build(tmp_path, garmin_days=BOTH, apple_days=BOTH)
    for tool in ("get_metric_history", "detect_metric_anomalies", "calculate_metric_trend"):
        result = await _call(db, tool, {"metric": "hrv_ms", **WINDOW})
        assert result["data_health"]["status"] == "MIXED_SOURCES", tool
        assert "unranked_source_conflict_in_window" in result["data_health"]["reasons"], tool


async def test_a_device_switch_inside_one_trend_window_is_reported(tmp_path):
    db = _build(tmp_path, garmin_days=range(29, 14, -1), apple_days=range(14, -1, -1))
    result = await _call(db, "calculate_metric_trend", {"metric": "hrv_ms", **WINDOW})
    assert result["data_health"]["status"] == "MIXED_SOURCES"
    assert result["data_health"]["reasons"] == ["source_changed_within_window"]


async def test_a_conflicting_series_is_reported_on_a_correlation(tmp_path):
    db = _build(tmp_path, garmin_days=BOTH, apple_days=BOTH, with_sleep=True)
    result = await _call(db, "find_metric_correlation", {"metric_a": "sleep_hours", "metric_b": "hrv_ms", **WINDOW})
    assert result["data_health"]["status"] == "MIXED_SOURCES"


async def test_an_explicit_source_priority_is_a_documented_rule_and_is_not_flagged(tmp_path):
    db = _build(tmp_path, garmin_days=BOTH, apple_days=BOTH, priority=[APPLE, GARMIN])
    result = await _call(db, "get_baseline", {"metric": "hrv_ms", **WINDOW})
    assert result["baseline"]["mean"] == 51.0  # always the preferred source's days
    assert result["data_health"]["status"] == "VALID"
    assert result["data_health"]["reasons"] == []


async def test_a_single_source_stays_valid(tmp_path):
    db = _build(tmp_path, apple_days=BOTH)
    for tool in ("get_baseline", "calculate_metric_trend", "get_metric_history"):
        result = await _call(db, tool, {"metric": "hrv_ms", **WINDOW})
        assert result["data_health"]["status"] == "VALID", tool
        assert result["data_health"]["reasons"] == [], tool


def _evidence(**overrides):
    base = {
        "observed_days": 30,
        "freshness_days": 0,
        "gaps": [],
        "observed_end": _iso(0),
        "observed_start": _iso(29),
        "requested_start": _iso(29),
        "requested_end": _iso(0),
        "expected_days": 30,
        "coverage_ratio": 1.0,
        "missing_days": 0,
        "measurement_count": 30,
    }
    return {**base, **overrides}


def test_source_issues_make_the_status_mixed_sources_and_are_listed_as_reasons():
    health = compose_data_health(_evidence(), None, source_issues=["source_changed_within_window"])
    assert health["status"] == "MIXED_SOURCES"
    assert "source_changed_within_window" in health["reasons"]


def test_mixed_sources_ranks_between_stale_and_gaps():
    stale = compose_data_health(_evidence(freshness_days=9), None, source_issues=["x"])
    gaps = compose_data_health(_evidence(gaps=[{"start": "a", "end": "b", "days": 1}]), None, source_issues=["x"])
    assert stale["status"] == "STALE"
    assert gaps["status"] == "MIXED_SOURCES"
    rank = SEVERITY_ORDER.index
    assert rank("STALE") < rank("MIXED_SOURCES") < rank("VALID_WITH_GAPS")


def test_no_source_issues_leaves_the_status_exactly_as_before():
    assert compose_data_health(_evidence(), None)["status"] == "VALID"
