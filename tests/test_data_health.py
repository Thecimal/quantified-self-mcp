"""
Tests for the data-quality state attached to get_metric_history and
get_baseline (data_health.py, schemas.DataHealth).
"""

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastmcp import Client

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_health import STATUSES, compose_data_health  # noqa: E402
from evidence import build_evidence  # noqa: E402
from logic import connect_writable, record_import_start  # noqa: E402


def _evidence(**over):
    base = {
        "requested_start": "2026-09-01",
        "requested_end": "2026-09-10",
        "observed_start": "2026-09-01",
        "observed_end": "2026-09-10",
        "expected_days": 10,
        "observed_days": 10,
        "coverage_ratio": 1.0,
        "missing_days": 0,
        "measurement_count": 10,
        "gaps": [],
        "freshness_days": 0,
        "recent_gap_days": 0,
        "confidence": "high",
    }
    return {**base, **over}


def _dataset(status="CURRENT", reason=None, **over):
    base = {
        "status": status,
        "reason": reason,
        "days_behind": 0,
        "last_successful_import": "2026-10-03T08:00:00",
        "latest_import": {
            "importer": "csv",
            "source_file": "health.csv",
            "status": "succeeded",
            "finished_at": "2026-10-03T08:00:00",
            "source_sha256": "x" * 64,
        },
    }
    return {**base, **over}


def test_complete_current_window_is_valid():
    health = compose_data_health(_evidence(), _dataset())
    assert health["status"] == "VALID"
    assert health["reasons"] == []
    assert health["observations"] == 10
    assert health["completeness"] == {
        "expected_days": 10,
        "observed_days": 10,
        "coverage_ratio": 1.0,
        "missing_days": 0,
    }


def test_gaps_make_it_valid_with_gaps_and_are_passed_through():
    gaps = [{"start": "2026-09-04", "end": "2026-09-05", "days": 2}]
    health = compose_data_health(_evidence(gaps=gaps, observed_days=8, missing_days=2), _dataset())
    assert health["status"] == "VALID_WITH_GAPS"
    assert health["reasons"] == ["gaps_in_window"]
    assert health["gaps"] == gaps


def test_stale_threshold_is_exclusive():
    assert compose_data_health(_evidence(freshness_days=2), _dataset())["status"] == "VALID"
    stale = compose_data_health(_evidence(freshness_days=3), _dataset())
    assert stale["status"] == "STALE"
    assert stale["freshness"]["age_days"] == 3
    assert compose_data_health(_evidence(freshness_days=3), _dataset(), stale_after_days=5)["status"] == "VALID"


def test_empty_window_is_insufficient_data():
    evidence = build_evidence([], date(2026, 9, 1), date(2026, 9, 10))
    health = compose_data_health(evidence, _dataset())
    assert health["status"] == "INSUFFICIENT_DATA"
    assert health["reasons"][0] == "no_observations_in_window"
    assert health["freshness"]["latest_data"] is None
    assert health["observations"] == 0


def test_insufficient_claim_tier_is_insufficient_data_and_keeps_its_reasons():
    health = compose_data_health(
        _evidence(), _dataset(), decision_tier="insufficient", must_state=["baseline_too_short"]
    )
    assert health["status"] == "INSUFFICIENT_DATA"
    assert health["reasons"] == ["baseline_too_short"]
    bare = compose_data_health(_evidence(), _dataset(), decision_tier="insufficient")
    assert bare["reasons"] == ["claim_tier_insufficient"]
    assert compose_data_health(_evidence(), _dataset(), decision_tier="suggestive")["status"] == "VALID"


def test_failed_or_interrupted_import_takes_precedence_and_lists_every_reason():
    gaps = [{"start": "2026-09-04", "end": "2026-09-05", "days": 2}]
    dataset = _dataset("IMPORT_FAILED", "import_interrupted")
    health = compose_data_health(_evidence(gaps=gaps, freshness_days=9), dataset)
    assert health["status"] == "IMPORT_INCOMPLETE"
    assert health["reasons"] == ["import_interrupted", "latest_observation_older_than_threshold", "gaps_in_window"]


def test_unreadable_dataset_status_is_reported_not_guessed():
    health = compose_data_health(_evidence(), None)
    assert health["status"] == "VALID"
    assert health["reasons"] == ["dataset_status_unavailable"]
    assert health["last_import"] is None and health["last_successful_import"] is None
    assert health["freshness"]["dataset_days_behind"] is None


def test_last_import_is_a_slim_record_without_the_hash():
    health = compose_data_health(_evidence(), _dataset(days_behind=1))
    assert health["last_import"] == {
        "importer": "csv",
        "status": "succeeded",
        "finished_at": "2026-10-03T08:00:00",
        "source_file": "health.csv",
    }
    assert health["last_successful_import"] == "2026-10-03T08:00:00"
    assert health["freshness"]["dataset_days_behind"] == 1


def test_every_status_the_function_can_return_is_in_the_documented_vocabulary():
    outcomes = {
        compose_data_health(_evidence(), _dataset())["status"],
        compose_data_health(_evidence(gaps=[{"start": "a", "end": "b", "days": 1}]), _dataset())["status"],
        compose_data_health(_evidence(observed_days=0), _dataset())["status"],
        compose_data_health(_evidence(freshness_days=9), _dataset())["status"],
        compose_data_health(_evidence(), _dataset("IMPORT_FAILED", "import_failed"))["status"],
    }
    assert outcomes == set(STATUSES)


@pytest.fixture
def health_db(tmp_path, monkeypatch):
    monkeypatch.setenv("HEALTH_DB_PATH", str(tmp_path / "health.db"))
    for mod in ("server", "privacy", "tools.health", "tools.measurements", "tools.workouts", "tools.status"):
        sys.modules.pop(mod, None)
    import server

    return server


async def _log_steps(client, days_ago):
    for offset in days_ago:
        day = (date.today() - timedelta(days=offset)).isoformat()
        await client.call_tool("log_daily_metric", {"date": day, "steps": 5000 + offset})


async def test_history_over_a_fully_logged_recent_window_is_valid(health_db):
    today = date.today()
    async with Client(health_db.mcp) as client:
        await _log_steps(client, range(5))
        result = await client.call_tool(
            "get_metric_history",
            {"metric": "steps", "start_date": (today - timedelta(days=4)).isoformat(), "end_date": today.isoformat()},
        )
    health = result.structured_content["data_health"]
    assert health["status"] == "VALID"
    assert health["observations"] == 5
    assert health["freshness"] == {"latest_data": today.isoformat(), "age_days": 0, "dataset_days_behind": 0}
    assert health["last_import"] is None


async def test_history_with_only_old_data_is_stale_and_also_reports_gaps(health_db):
    async with Client(health_db.mcp) as client:
        await _log_steps(client, [10])
        result = await client.call_tool("get_metric_history", {"metric": "steps"})
    health = result.structured_content["data_health"]
    assert health["status"] == "STALE"
    assert health["freshness"]["age_days"] == 10
    assert "gaps_in_window" in health["reasons"]


async def test_baseline_over_a_short_window_is_insufficient_data_with_the_claim_reasons(health_db):
    today = date.today()
    async with Client(health_db.mcp) as client:
        await _log_steps(client, range(5))
        result = await client.call_tool(
            "get_baseline",
            {"metric": "steps", "start_date": (today - timedelta(days=4)).isoformat(), "end_date": today.isoformat()},
        )
    sc = result.structured_content
    assert sc["claim"]["decision"]["tier"] == "insufficient"
    assert sc["data_health"]["status"] == "INSUFFICIENT_DATA"
    assert set(sc["claim"]["decision"]["must_state"]) <= set(sc["data_health"]["reasons"])


async def test_an_unfinished_import_makes_both_tools_report_import_incomplete(health_db, tmp_path):
    source = tmp_path / "export.csv"
    source.write_text("date,steps\n2026-01-01,1\n", encoding="utf-8")
    async with Client(health_db.mcp) as client:
        await _log_steps(client, range(3))
        conn = connect_writable(health_db.HEALTH_DB_PATH)
        record_import_start(conn, "csv", source)
        conn.close()
        history = await client.call_tool("get_metric_history", {"metric": "steps"})
        baseline = await client.call_tool("get_baseline", {"metric": "steps"})
    for result in (history, baseline):
        health = result.structured_content["data_health"]
        assert health["status"] == "IMPORT_INCOMPLETE"
        assert health["reasons"][0] == "import_interrupted"
        assert health["last_import"]["status"] == "running"


async def test_data_health_is_in_the_output_schema_of_exactly_the_analytics_tools(health_db):
    async with Client(health_db.mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
    carrying = {name for name, tool in tools.items() if "data_health" in str(tool.output_schema)}
    assert carrying == {
        "get_metric_history",
        "get_baseline",
        "detect_metric_anomalies",
        "calculate_metric_trend",
        "compare_metric_periods",
        "find_metric_correlation",
        "get_recent_changes",
        "explain_metric_change",
    }
