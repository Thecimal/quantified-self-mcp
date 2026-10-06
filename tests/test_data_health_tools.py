"""
data_health on every analytics tool, and merge_data_health for results that rest on several windows.
The fixtures are the agent-eval ones, so each promised state is checked against the real server.
"""

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastmcp import Client

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from data_health import SEVERITY_ORDER, STATUSES, merge_data_health  # noqa: E402
from eval.agent.fixtures import FIXTURES, build_fixture  # noqa: E402
from eval.agent.runner import loaded_server  # noqa: E402

TODAY = date.today()


def _iso(days_ago):
    return (TODAY - timedelta(days=days_ago)).isoformat()


TOOLS = {
    "detect_metric_anomalies": {"metric": "hrv_ms"},
    "calculate_metric_trend": {"metric": "sleep_hours"},
    "compare_metric_periods": {
        "metric": "steps",
        "period_a_start": _iso(29),
        "period_a_end": _iso(0),
        "period_b_start": _iso(59),
        "period_b_end": _iso(30),
    },
    "find_metric_correlation": {"metric_a": "sleep_hours", "metric_b": "hrv_ms"},
    "get_recent_changes": {},
    "explain_metric_change": {"metric": "hrv_ms", "date": _iso(3)},
}


async def _call(fixture_name, tool, arguments, tmp_path):
    fixture = FIXTURES[fixture_name]
    db_path = tmp_path / f"{fixture_name}.db"
    build_fixture(fixture, db_path, TODAY)
    with loaded_server(db_path, fixture.private_fields) as server:
        async with Client(server.mcp) as client:
            result = await client.call_tool(tool, arguments)
    return result.structured_content


def _health(status, reasons=(), **extra):
    return {"status": status, "reasons": list(reasons), **extra}


# ---- merge_data_health -------------------------------------------------------------------------------------


def test_severity_order_covers_exactly_the_documented_statuses():
    assert sorted(SEVERITY_ORDER) == sorted(STATUSES)


def test_the_weakest_window_wins_and_every_reason_is_listed_once():
    merged = merge_data_health(
        [
            _health("VALID_WITH_GAPS", ["gaps_in_window"], observations=10),
            _health("STALE", ["latest_observation_older_than_threshold", "gaps_in_window"], observations=7),
            _health("VALID", [], observations=30),
        ]
    )
    assert merged["status"] == "STALE" and merged["observations"] == 7
    assert merged["reasons"] == ["latest_observation_older_than_threshold", "gaps_in_window"]


@pytest.mark.parametrize("weaker", SEVERITY_ORDER[:-1])
def test_every_status_outranks_valid(weaker):
    assert merge_data_health([_health("VALID"), _health(weaker)])["status"] == weaker


def test_a_single_window_is_returned_unchanged_and_an_empty_list_is_rejected():
    single = _health("VALID", [], observations=3)
    assert merge_data_health([single]) == single
    with pytest.raises(ValueError):
        merge_data_health([])


def test_ties_keep_the_first_window_and_do_not_mutate_the_inputs():
    first = _health("STALE", ["a"], observations=1)
    second = _health("STALE", ["b"], observations=2)
    merged = merge_data_health([first, second])
    assert merged["observations"] == 1 and merged["reasons"] == ["a", "b"]
    assert first["reasons"] == ["a"] and second["reasons"] == ["b"]


# ---- every analytics tool reports it -----------------------------------------------------------------------


@pytest.mark.parametrize("tool", TOOLS)
async def test_every_analytics_tool_reports_data_health_on_complete_data(tool, tmp_path):
    health = (await _call("complete", tool, TOOLS[tool], tmp_path))["data_health"]
    assert health["status"] in ("VALID", "VALID_WITH_GAPS")
    assert health["observations"] >= 1 and health["completeness"]["expected_days"] >= 1
    assert health["last_import"]["status"] == "succeeded"


@pytest.mark.parametrize("tool", TOOLS)
async def test_a_failed_import_is_reported_by_every_analytics_tool(tool, tmp_path):
    health = (await _call("import_failed", tool, TOOLS[tool], tmp_path))["data_health"]
    assert health["status"] == "IMPORT_INCOMPLETE"
    assert health["reasons"][0] == "import_failed"


@pytest.mark.parametrize("tool", TOOLS)
async def test_stale_data_is_reported_by_every_analytics_tool(tool, tmp_path):
    health = (await _call("stale", tool, TOOLS[tool], tmp_path))["data_health"]
    assert health["status"] in ("STALE", "INSUFFICIENT_DATA")
    assert "latest_observation_older_than_threshold" in health["reasons"]
    assert health["freshness"]["age_days"] >= 3


# ---- results resting on several windows report the weakest ------------------------------------------------


async def test_a_comparison_against_an_empty_period_is_insufficient(tmp_path):
    arguments = {**TOOLS["compare_metric_periods"], "period_b_start": _iso(400), "period_b_end": _iso(371)}
    result = await _call("complete", "compare_metric_periods", arguments, tmp_path)
    health = result["data_health"]
    assert health["status"] == "INSUFFICIENT_DATA"
    assert "no_observations_in_window" in health["reasons"]
    assert result["claim"]["decision"]["tier"] == "insufficient"


async def test_a_correlation_with_a_metric_that_has_no_data_is_insufficient(tmp_path):
    arguments = {"metric_a": "sleep_hours", "metric_b": "water_ml"}
    health = (await _call("complete", "find_metric_correlation", arguments, tmp_path))["data_health"]
    assert health["status"] == "INSUFFICIENT_DATA"
    assert "no_observations_in_window" in health["reasons"]


async def test_recent_changes_on_an_empty_database_is_insufficient_not_silent(tmp_path):
    result = await _call("empty", "get_recent_changes", {}, tmp_path)
    assert result["changes"] == []
    assert result["data_health"]["status"] == "INSUFFICIENT_DATA"
    assert result["data_health"]["observations"] == 0


async def test_an_explanation_over_sparse_data_carries_its_decision_reasons(tmp_path):
    result = await _call("sparse", "explain_metric_change", {"metric": "hrv_ms", "date": _iso(0)}, tmp_path)
    assert result["overall_decision"]["tier"] == "insufficient"
    health = result["data_health"]
    assert health["status"] == "INSUFFICIENT_DATA"
    assert set(result["overall_decision"]["must_state"]) <= set(health["reasons"])


async def test_correlated_metrics_nested_in_an_explanation_do_not_repeat_data_health(tmp_path):
    result = await _call("complete", "explain_metric_change", {"metric": "hrv_ms", "date": _iso(0)}, tmp_path)
    assert result["data_health"] is not None
    assert all(item["data_health"] is None for item in result["correlated_metrics"])
