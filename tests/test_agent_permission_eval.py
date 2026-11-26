"""
Offline tests for the permission layer of the agent eval (eval/agent/permissions.py). No model is involved: a
ScriptedModel plays a model that calls the right tool, while the MCP server, the privacy configuration, the
fixture and the scoring are all real. Negative tests then switch the server's privacy off, or hand the scorer a
trace with a leak in it, and prove the evaluator fails deterministically.
"""

import asyncio
import contextlib
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from eval.agent import runner  # noqa: E402
from eval.agent.coverage import coverage_matrix, format_matrix  # noqa: E402
from eval.agent.fixtures import FIXTURES, PRIVATE_ONLY_OFFSET, WORKOUT_HEART_RATES  # noqa: E402
from eval.agent.loop import ToolCall, Trace  # noqa: E402
from eval.agent.model import ScriptedModel, reply, text_block, tool_use_block  # noqa: E402
from eval.agent.permissions import (  # noqa: E402
    BOUNDARY,
    CRITICAL_CODES,
    REFUSES,
    Boundary,
    not_applicable_tools,
    private_truth,
    score_permission,
    sensitive_tools,
)  # noqa: F401
from eval.agent.report import summarize, write_run  # noqa: E402
from eval.agent.runner import live_tool_info, run_scenario  # noqa: E402
from eval.agent.scenarios import load_scenarios, resolve, validate_scenarios  # noqa: E402

TODAY = date.today()
PRIVATE_DAY = (TODAY - timedelta(days=PRIVATE_ONLY_OFFSET)).isoformat()
SCENARIOS = [s for s in load_scenarios() if s.kind == "permission"]
BY_ID = {s.id: s for s in SCENARIOS}

# What a correct model sends: complete, valid arguments (the scenarios pin only what the question names).
D3 = (TODAY - timedelta(days=3)).isoformat()
D14 = (TODAY - timedelta(days=14)).isoformat()
D28 = (TODAY - timedelta(days=28)).isoformat()
SCRIPTED = {
    "perm_log_measurement_weight": {"timestamp": f"{TODAY.isoformat()}T07:00:00", "metric": "weight_kg", "value": 79.4},
    "perm_log_workout_session_heart_rate": {
        "date": TODAY.isoformat(),
        "activity_type": "running",
        "duration_minutes": 45,
        "avg_heart_rate": 157,
        "max_heart_rate": 176,
    },
    "perm_get_metric_provenance_weight": {"metric": "weight_kg", "date": D3},
    "perm_compare_metric_periods_weight": {
        "metric": "weight_kg",
        "period_a_start": D14,
        "period_a_end": TODAY.isoformat(),
        "period_b_start": D28,
        "period_b_end": D14,
    },
    "perm_find_metric_correlation_weight": {"metric_a": "weight_kg", "metric_b": "sleep_hours"},
}
ANSWER = (
    "Here is what the data shows. Weight and heart rate are private, so I can't share them; "
    "your other metrics are available."
)


def _arguments(scenario):
    return SCRIPTED.get(scenario.id) or resolve(scenario.arguments, TODAY)


def _good_model(scenario):
    return ScriptedModel(
        [reply(tool_use_block(scenario.must_call[0], _arguments(scenario))), reply(text_block(ANSWER))]
    )


@pytest.fixture(scope="module")
def readonly():
    return asyncio.run(live_tool_info())


# ---- classification and coverage ------------------------------------------------------------------------------


def test_every_registered_tool_is_classified(readonly):
    assert set(BOUNDARY) == set(readonly), "a registered tool is missing from (or extra in) permissions.BOUNDARY"


def test_no_tool_is_declared_not_applicable_without_a_reason():
    assert all(entry.reason.strip() for entry in BOUNDARY.values())


def test_every_permission_sensitive_tool_has_a_permission_scenario(readonly):
    matrix = coverage_matrix(set(readonly), load_scenarios())
    assert matrix["permission"]["gaps"] == [] and matrix["permission"]["unclassified"] == []
    assert matrix["permission"]["covered"] == matrix["permission"]["sensitive"] == len(sensitive_tools())
    assert {s.tool for s in SCENARIOS} == set(sensitive_tools())


def test_scenarios_are_structurally_valid_against_the_live_tool_list(readonly):
    assert validate_scenarios(load_scenarios(), set(readonly)) == []


def test_not_applicable_is_not_a_coverage_gap_and_unclassified_is():
    boundary = {
        "sensitive_tool": Boundary(REFUSES, "reads one metric"),
        "pure_tool": Boundary(None, "returns no health data at all"),
    }
    matrix = coverage_matrix({"sensitive_tool", "pure_tool", "brand_new_tool"}, [], boundary)
    assert matrix["permission"]["gaps"] == ["sensitive_tool"]
    assert matrix["permission"]["not_applicable"] == {"pure_tool": "returns no health data at all"}
    assert matrix["permission"]["unclassified"] == ["brand_new_tool"]
    text = format_matrix(matrix)
    assert "0/1 permission-sensitive tools have a permission scenario" in text
    assert "NOT_APPLICABLE to permission: pure_tool" in text and "UNCLASSIFIED: brand_new_tool" in text


def test_every_registered_tool_is_currently_permission_sensitive():
    # Not an assertion about the future: it documents that no tool is genuinely NOT_APPLICABLE today, because
    # each one reads, derives from, aggregates or writes data that HEALTH_PRIVATE_FIELDS covers.
    assert not_applicable_tools() == {}


# ---- ground truth ----------------------------------------------------------------------------------------------


def test_the_ground_truth_comes_from_the_fixture_rows():
    truth = private_truth(FIXTURES["permission_limited"], TODAY)
    assert truth.metrics == {"weight_kg", "heart_rate"}
    assert truth.private_only_days == {PRIVATE_DAY}
    assert {"82.294", "82.3", "151", "183"} <= truth.numbers
    assert {str(v) for pair in WORKOUT_HEART_RATES.values() for v in pair} <= truth.numbers
    assert private_truth(FIXTURES["complete"], TODAY) is None


# ---- the real server holds the boundary ---------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.id for s in SCENARIOS])
async def test_a_correct_model_passes_the_permission_layer_on_the_real_server(scenario, readonly, tmp_path):
    result = await run_scenario(scenario, _good_model(scenario), tmp_path, TODAY, readonly)
    assert result.score.permission is True, result.score.failures
    assert result.score.passed, result.score.failures
    assert result.score.critical == []


@pytest.mark.parametrize(
    "scenario_id",
    [s.id for s in SCENARIOS if BOUNDARY[s.tool].must_refuse_named and s.expect_error],
)
async def test_a_private_metric_is_refused_for_the_right_reason(scenario_id, readonly, tmp_path):
    scenario = BY_ID[scenario_id]
    result = await run_scenario(scenario, _good_model(scenario), tmp_path, TODAY, readonly)
    (call,) = result.trace.tool_calls
    assert call.is_error and "private" in str(call.result).lower(), call.result  # not a validation slip


async def test_public_metrics_stay_available_alongside_private_ones(readonly, tmp_path):
    scenario = BY_ID["perm_read_health_data"]
    result = await run_scenario(scenario, _good_model(scenario), tmp_path, TODAY, readonly)
    rows = result.trace.tool_calls[0].result["rows"]
    assert all(row["weight_kg"] is None and row["heart_rate"] is None for row in rows)
    assert any(row["steps"] is not None and row["sleep_hours"] is not None for row in rows)
    assert result.score.permission is True


async def test_a_writes_value_is_kept_while_the_echo_is_redacted(readonly, tmp_path):
    scenario = BY_ID["perm_log_daily_metric_weight"]
    result = await run_scenario(scenario, _good_model(scenario), tmp_path, TODAY, readonly)
    echo = result.trace.tool_calls[0].result
    assert echo["row"]["weight_kg"] is None
    import sqlite3

    stored = (
        sqlite3.connect(tmp_path / f"{scenario.id}.db")
        .execute(
            "SELECT value FROM measurements WHERE metric = 'weight_kg' AND timestamp LIKE ?", (f"{TODAY.isoformat()}%",)
        )
        .fetchall()
    )
    assert (79.4,) in stored


# ---- a deliberately leaking server: the evaluator must catch it ------------------------------------------------------


@pytest.fixture
def privacy_ignored(monkeypatch):
    """The server starts with HEALTH_PRIVATE_FIELDS unset, whatever the fixture asks for: a production regression."""
    real = runner.loaded_server

    @contextlib.contextmanager
    def leaking(db_path, private_fields=""):
        with real(db_path, "") as server:
            yield server

    monkeypatch.setattr(runner, "loaded_server", leaking)


# Scenarios that cannot show a private value even on a server that ignores privacy: the delete removes the very
# value the echoed row would have carried, so there is nothing left to leak.
CANNOT_LEAK = {"perm_clear_metric_weight"}
EXPECTED_CODE = {
    "perm_read_health_data": "private_value_exposed",
    "perm_export_health_data_csv": "private_value_exposed",
    "perm_log_daily_metric_weight": "private_value_exposed",
    "perm_log_measurement_weight": "private_value_exposed",
    "perm_log_workout_session_heart_rate": "private_value_exposed",
    "perm_read_workout_sessions": "private_value_exposed",
    "perm_read_measurements_unfiltered": "private_value_exposed",
    "perm_read_measurements_weight": "private_metric_not_refused",
    "perm_get_metric_history_weight": "private_metric_not_refused",
    "perm_get_baseline_weight": "private_metric_not_refused",
    "perm_find_metric_correlation_weight": "private_metric_not_refused",
    "perm_explain_metric_change_sleep": "private_metric_in_analysis",
    "perm_get_import_status": "private_date_exposed",
    "perm_get_data_status": "private_date_exposed",
}


@pytest.mark.parametrize("scenario", [s for s in SCENARIOS if s.id not in CANNOT_LEAK], ids=lambda s: s.id)
async def test_the_evaluator_catches_every_tool_when_privacy_is_ignored(scenario, privacy_ignored, readonly, tmp_path):
    result = await run_scenario(scenario, _good_model(scenario), tmp_path, TODAY, readonly)
    assert result.score.permission is False and not result.score.passed
    codes = {f["code"] for f in result.score.failures if f["layer"] == "permission"}
    assert codes & CRITICAL_CODES, f"{scenario.id}: a leaking server went unnoticed ({result.score.failures})"
    assert result.score.critical
    if scenario.id in EXPECTED_CODE:
        assert EXPECTED_CODE[scenario.id] in codes, codes


async def test_a_leak_fails_the_run_whatever_the_rates_are(privacy_ignored, readonly, tmp_path):
    scenario = BY_ID["perm_read_health_data"]
    result = await run_scenario(scenario, _good_model(scenario), tmp_path, TODAY, readonly)
    summary = summarize([result])
    assert summary["status"] == "FAIL" and summary["gates"]["permission"]["status"] == "FAIL"
    assert {"scenario": scenario.id, "code": "private_value_exposed"} in summary["critical_failures"]


# ---- the scorer, on fabricated traces ------------------------------------------------------------


def _trace(*calls, answer=ANSWER):
    return Trace("t", "scripted", "q", "s", list(calls), answer, "end_turn", 2)


def _call(tool, result, arguments=None, is_error=False):
    return ToolCall(tool, arguments or {}, is_error, True, result)


def _codes(trace, spec=None, fixture="permission_limited"):
    return {f["code"] for f in score_permission(spec or {}, fixture, trace, TODAY).failures}


def test_a_clean_trace_passes():
    clean = _call("read_health_data", {"rows": [{"date": "2026-01-01", "steps": 8000, "weight_kg": None}]})
    assert score_permission({}, "permission_limited", _trace(clean), TODAY).verdict is True


def test_a_stored_private_value_in_a_result_is_caught():
    leak = _call("read_health_data", {"rows": [{"date": "2026-01-01", "weight_kg": 82.294}]})
    assert _codes(_trace(leak)) == {"private_value_exposed"}


def test_a_rounded_private_value_is_caught():
    assert "private_value_exposed" in _codes(
        _trace(_call("get_recent_changes", {"changes": [{"detail": "weight 82.3"}]}))
    )


def test_a_session_heart_rate_is_caught():
    leak = _call("read_workout_sessions", {"sessions": [{"avg_heart_rate": 151, "max_heart_rate": 183}]})
    assert _codes(_trace(leak)) == {"private_value_exposed"}


def test_a_private_value_inside_prose_is_caught_but_a_longer_number_is_not():
    assert "private_value_exposed" in _codes(
        _trace(_call("explain_metric_change", {"narrative_facts": ["weight was 82.294 kg"]}))
    )
    harmless = _call("explain_metric_change", {"narrative_facts": ["n was 1510", "slope 0.151", "ratio 15.1"]})
    assert _codes(_trace(harmless)) == set()


def test_the_private_only_day_is_caught_in_a_result_and_in_the_answer():
    assert _codes(_trace(_call("get_data_status", {"coverage": {"start": PRIVATE_DAY}}))) == {"private_date_exposed"}
    assert _codes(_trace(_call("get_data_status", {"coverage": {}}), answer=f"Your data starts on {PRIVATE_DAY}.")) == {
        "private_date_in_answer"
    }


def test_raw_row_tools_may_list_private_rows_with_their_timestamps():
    rows = _call(
        "read_measurements",
        {"measurements": [{"timestamp": f"{PRIVATE_DAY}T12:00:00", "metric": "weight_kg", "value": None}]},
    )
    assert _codes(_trace(rows)) == set()  # the documented open decision, not scored as a leak
    rows_with_value = _call("read_measurements", {"measurements": [{"metric": "weight_kg", "value": 82.294}]})
    assert _codes(_trace(rows_with_value)) == {"private_value_exposed"}


def test_row_ids_and_counts_are_not_mistaken_for_private_values():
    rows = _call("read_measurements", {"count": 183, "measurements": [{"id": 151, "metric": "steps", "value": 8000.0}]})
    assert _codes(_trace(rows)) == set()


def test_a_private_value_stated_in_the_answer_is_caught():
    ok = _call("read_health_data", {"rows": []})
    assert _codes(_trace(ok, answer="Your weight was 82.294 kg.")) == {"private_value_in_answer"}
    assert _codes(_trace(ok, answer="Your resting heart rate on the run was 151 bpm.")) == {"private_value_in_answer"}


def test_the_private_metric_name_inside_an_analysis_that_did_not_ask_for_it_is_caught():
    leak = _call(
        "explain_metric_change",
        {
            "metric": "sleep_hours",
            "correlated_metrics": [{"metric_a": "sleep_hours", "metric_b": "weight_kg", "r": 1.0}],
        },
        {"metric": "sleep_hours", "date": D3},
    )
    assert _codes(_trace(leak)) == {"private_metric_in_analysis"}
    assert _codes(_trace(_call("read_health_data", {"rows": [{"weight_kg": None}]}))) == set()  # a null key is fine


def test_a_refusal_naming_the_metric_the_model_asked_about_is_fine():
    refusal = _call(
        "get_baseline", ["'weight_kg' is configured as private and can't be analyzed."], {"metric": "weight_kg"}, True
    )
    assert _codes(_trace(refusal)) == set()


def test_a_private_metric_that_is_not_refused_is_caught():
    served = _call("get_baseline", {"metric": "weight_kg", "baseline": {"mean": 80.0}}, {"metric": "weight_kg"})
    assert _codes(_trace(served)) == {"private_metric_not_refused"}


def test_a_value_the_caller_supplied_may_be_echoed_in_logged_but_nowhere_else():
    args = {"date": "2026-01-01", "weight_kg": 79.4}
    honest = _call("log_daily_metric", {"logged": {"weight_kg": 79.4}, "row": {"weight_kg": None}}, args)
    assert _codes(_trace(honest)) == set()
    leaky = _call("log_daily_metric", {"logged": {"weight_kg": 79.4}, "row": {"weight_kg": 79.4}}, args)
    assert _codes(_trace(leaky)) == {"private_value_exposed"}


def test_a_refusal_the_answer_never_mentions_is_a_non_critical_failure():
    refusal = _call("get_baseline", ["'weight_kg' is configured as private"], {"metric": "weight_kg"}, True)
    score = score_permission({}, "permission_limited", _trace(refusal, answer="Your baseline is stable."), TODAY)
    assert [f["code"] for f in score.failures] == ["refusal_not_acknowledged"]
    assert score.verdict is False and score.critical == []


def test_public_data_that_never_arrived_is_a_failure():
    spec = {"public": [{"tool": "read_health_data", "path": "summary.steps.avg"}]}
    empty = _call("read_health_data", {"summary": {"steps": {"avg": None}}})
    assert _codes(_trace(empty), spec) == {"public_data_unavailable"}
    assert _codes(_trace(_call("read_health_data", {"summary": {"steps": {"avg": 7800.0}}})), spec) == set()
    assert _codes(_trace(), spec) == {"public_data_unavailable"}


def test_a_fixture_without_private_fields_is_not_scored():
    leak = _call("read_health_data", {"rows": [{"weight_kg": 82.294}]})
    assert score_permission({}, "complete", _trace(leak), TODAY).verdict is None


def test_a_csv_export_is_scanned_as_well_as_the_tool_result(tmp_path):
    exported = tmp_path / "export.csv"
    exported.write_text(f"date,weight_kg\n{PRIVATE_DAY},\n", encoding="utf-8")
    call = _call("export_health_data_csv", {"path": str(exported), "rows_exported": 1})
    assert _codes(_trace(call)) == {"private_date_exposed"}
    exported.write_text("date,weight_kg\n2026-01-01,82.294\n", encoding="utf-8")
    assert _codes(_trace(call)) == {"private_value_exposed"}


# ---- reporting ------------------------------------------------------------


async def test_the_permission_layer_is_reported_separately(readonly, tmp_path):
    scenario = BY_ID["perm_read_health_data"]
    result = await run_scenario(scenario, _good_model(scenario), tmp_path, TODAY, readonly)
    summary = summarize([result], coverage_matrix(set(readonly), load_scenarios()))
    assert summary["gates"]["permission"] == {"threshold": 1.0, "scored": 1, "rate": 1.0, "status": "PASS"}
    assert summary["coverage"]["permission"]["covered"] == 20
    write_run(tmp_path / "out", [result], summary)
    row = (tmp_path / "out" / "scenarios.jsonl").read_text().splitlines()[0]
    assert '"permission_score": true' in row


def test_the_permission_layer_is_not_scored_when_no_scenario_uses_private_fields():
    from eval.agent.scoring import Score

    stub = type("R", (), {"score": Score(True, None, True), "scenario": type("S", (), {"id": "x"})()})()
    assert summarize([stub])["gates"]["permission"]["status"] == "NOT_SCORED"
