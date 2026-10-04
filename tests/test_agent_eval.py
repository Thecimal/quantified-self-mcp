"""
Offline tests for the agent-eval harness in eval/agent. No model and no API key
are needed: a ScriptedModel (or a fake HTTP transport) stands in for the model,
while the MCP server, its tools, the fixtures and the scoring are all real.
"""

import asyncio
import io
import json
import os
import subprocess
import sys
import urllib.error
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastmcp import Client

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from eval.agent.coverage import coverage_matrix  # noqa: E402
from eval.agent.fixtures import FIXTURES, build_fixture  # noqa: E402
from eval.agent.loop import run_agent  # noqa: E402
from eval.agent.model import (  # noqa: E402
    API_URL,
    AnthropicHTTP,
    ModelError,
    ScriptedModel,
    reply,
    text_block,
    tool_use_block,
)
from eval.agent.report import summarize, write_run  # noqa: E402
from eval.agent.runner import live_tool_info, loaded_server, run_scenario  # noqa: E402
from eval.agent.scenarios import Scenario, load_scenarios, validate_scenarios  # noqa: E402
from eval.agent.scoring import Score  # noqa: E402

TODAY = date.today()


@pytest.fixture(scope="module")
def readonly():
    return asyncio.run(live_tool_info())


def _scenario(scenario_id):
    return next(s for s in load_scenarios() if s.id == scenario_id)


async def _run(scenario_id, responses, tmp_path, readonly):
    model = ScriptedModel(responses)
    result = await run_scenario(_scenario(scenario_id), model, tmp_path, TODAY, readonly)
    return result, model


# ---- scenarios and the coverage matrix ---------------------------------------------------------------------


def test_shipped_scenarios_validate_against_the_live_tool_list(readonly):
    assert validate_scenarios(load_scenarios(), set(readonly)) == []


def test_every_registered_tool_is_represented_by_a_scenario_of_its_own(readonly):
    matrix = coverage_matrix(set(readonly), load_scenarios())
    assert matrix["tools"] == len(readonly)
    assert matrix["represented"] == matrix["tools"]
    assert matrix["gaps"]["represented"] == []
    assert matrix["gaps"]["should_use"] == []


def test_a_tool_called_inside_other_scenarios_earns_no_coverage(readonly):
    scenarios = [s for s in load_scenarios() if s.tool != "get_baseline"]
    assert any("get_baseline" in (s.allowed_tools or []) for s in scenarios)
    assert coverage_matrix(set(readonly), scenarios)["gaps"]["represented"] == ["get_baseline"]


def test_a_newly_registered_tool_shows_up_as_a_gap(readonly):
    matrix = coverage_matrix({*readonly, "brand_new_tool"}, load_scenarios())
    assert matrix["gaps"]["represented"] == ["brand_new_tool"]
    assert matrix["tools"] == len(readonly) + 1


def test_validate_scenarios_reports_every_kind_of_structural_problem():
    good = _scenario("get_baseline_resting_hr")
    bad = [
        good,
        Scenario(**{**good.__dict__}),  # duplicate id
        Scenario(**{**good.__dict__, "id": "k", "kind": "nonsense"}),
        Scenario(**{**good.__dict__, "id": "f", "fixture": "nowhere"}),
        Scenario(**{**good.__dict__, "id": "m", "must_call": ["get_recent_changes"]}),
        Scenario(**{**good.__dict__, "id": "t", "allowed_tools": ["no_such_tool"]}),
    ]
    problems = "\n".join(validate_scenarios(bad, {"get_baseline", "get_recent_changes"}))
    for expected in ("duplicate id", "unknown kind", "unknown fixture", "must list its own tool", "no_such_tool"):
        assert expected in problems


# ---- fixtures keep their promises against the real server --------------------------------------------------


async def _call(fixture_name, tool, arguments, tmp_path):
    fixture = FIXTURES[fixture_name]
    db_path = tmp_path / f"{fixture_name}.db"
    build_fixture(fixture, db_path, TODAY)
    with loaded_server(db_path, fixture.private_fields) as server:
        async with Client(server.mcp) as client:
            return await client.call_tool(tool, arguments, raise_on_error=False)


async def test_complete_fixture_is_current_and_valid(tmp_path):
    status = (await _call("complete", "get_data_status", {}, tmp_path)).structured_content
    assert status["status"] == "CURRENT"
    assert status["latest_import"]["status"] == "succeeded"
    history = await _call("complete", "get_metric_history", {"metric": "steps"}, tmp_path)
    assert history.structured_content["data_health"]["status"] == "VALID"


async def test_incomplete_fixture_has_the_promised_gaps(tmp_path):
    start = (TODAY.toordinal() - 89, TODAY)
    arguments = {"metric": "steps", "start_date": date.fromordinal(start[0]).isoformat(), "end_date": TODAY.isoformat()}
    history = (await _call("incomplete", "get_metric_history", arguments, tmp_path)).structured_content
    assert history["data_health"]["status"] == "VALID_WITH_GAPS"
    assert history["data_health"]["completeness"]["missing_days"] == 17
    status = (await _call("incomplete", "get_data_status", {}, tmp_path)).structured_content
    assert (status["status"], status["gap_count"]) == ("INCOMPLETE", 2)


async def test_stale_fixture_is_stale(tmp_path):
    status = (await _call("stale", "get_data_status", {}, tmp_path)).structured_content
    assert (status["status"], status["days_behind"]) == ("STALE", 12)
    history = (await _call("stale", "get_metric_history", {"metric": "steps"}, tmp_path)).structured_content
    assert history["data_health"]["status"] == "STALE"


async def test_sparse_fixture_cannot_support_a_baseline(tmp_path):
    result = (await _call("sparse", "get_baseline", {"metric": "steps"}, tmp_path)).structured_content
    assert result["data_health"]["status"] == "INSUFFICIENT_DATA"
    assert result["data_health"]["observations"] == 5


async def test_empty_fixture_is_no_data(tmp_path):
    status = (await _call("empty", "get_data_status", {}, tmp_path)).structured_content
    assert status["status"] == "NO_DATA"


async def test_pagination_fixture_exceeds_one_read(tmp_path):
    start = date.fromordinal(TODAY.toordinal() - 419).isoformat()
    result = (
        await _call("pagination", "read_health_data", {"start_date": start, "end_date": TODAY.isoformat()}, tmp_path)
    ).structured_content
    assert result["truncated"] is True
    assert len(result["rows"]) == 400
    assert result["summary"]["days_with_data"] == 420


async def test_conflicting_fixture_has_two_disagreeing_sources(tmp_path):
    yesterday = date.fromordinal(TODAY.toordinal() - 1).isoformat()
    result = (
        await _call("conflicting", "get_metric_provenance", {"metric": "steps", "date": yesterday}, tmp_path)
    ).structured_content
    assert result["conflict"] is True
    assert {s["source"] for s in result["sources"]} == {"Apple Watch", "Garmin"}


async def test_permission_limited_fixture_refuses_analytics_and_redacts_reads(tmp_path):
    refused = await _call("permission_limited", "get_baseline", {"metric": "weight_kg"}, tmp_path)
    assert refused.is_error is True
    rows = (await _call("permission_limited", "read_health_data", {}, tmp_path)).structured_content["rows"]
    assert all(row["weight_kg"] is None for row in rows)
    assert any(row["steps"] is not None for row in rows)


async def test_import_failed_fixture_is_reported_by_both_status_and_data_health(tmp_path):
    status = (await _call("import_failed", "get_data_status", {}, tmp_path)).structured_content
    assert status["status"] == "IMPORT_FAILED"
    history = (await _call("import_failed", "get_metric_history", {"metric": "steps"}, tmp_path)).structured_content
    assert history["data_health"]["status"] == "IMPORT_INCOMPLETE"


# ---- the agent loop and scoring ----------------------------------------------------------------------------


async def test_a_correct_call_passes_every_scored_layer(tmp_path, readonly):
    result, model = await _run(
        "get_baseline_resting_hr",
        [reply(tool_use_block("get_baseline", {"metric": "resting_heart_rate"})), reply(text_block("done"))],
        tmp_path,
        readonly,
    )
    score = result.score
    assert score.passed and (score.routing, score.arguments, score.execution) == (True, True, True)
    assert (score.interpretation, score.grounding) == (None, None)
    assert result.trace.terminated == "end_turn" and result.trace.called_tools() == ["get_baseline"]
    assert result.trace.final_answer == "done"

    second_turn = model.calls[1]["messages"][-1]
    assert second_turn["role"] == "user"
    (tool_result,) = second_turn["content"]
    assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == "toolu_1"
    assert tool_result["is_error"] is False
    assert {"baseline", "claim", "data_health"} <= set(json.loads(tool_result["content"]))
    assert "2" in model.calls[0]["system"] and TODAY.isoformat() in model.calls[0]["system"]


async def test_the_wrong_tool_fails_routing_and_arguments_but_is_not_critical(tmp_path, readonly):
    responses = [reply(tool_use_block("get_recent_changes", {})), reply(text_block("x"))]
    result, _ = await _run("get_baseline_resting_hr", responses, tmp_path, readonly)
    codes = {f["code"] for f in result.score.failures}
    assert {"missing_required_tool", "unexpected_tool", "argument_mismatch"} <= codes
    assert result.score.routing is False and result.score.critical == []


async def test_a_hallucinated_tool_is_critical(tmp_path, readonly):
    result, _ = await _run(
        "get_baseline_resting_hr",
        [reply(tool_use_block("get_sleep_trend", {"metric": "sleep_hours"})), reply(text_block("x"))],
        tmp_path,
        readonly,
    )
    assert result.score.critical == ["tool_hallucination"]
    assert result.score.execution is False and not result.score.passed
    assert result.trace.tool_calls[0].registered is False and result.trace.tool_calls[0].is_error is True


async def test_a_write_tool_on_a_read_question_is_critical(tmp_path, readonly):
    arguments = {"date": TODAY.isoformat(), "steps": 1}
    result, _ = await _run(
        "get_baseline_resting_hr",
        [reply(tool_use_block("log_daily_metric", arguments)), reply(text_block("x"))],
        tmp_path,
        readonly,
    )
    assert result.score.critical == ["forbidden_tool_called"] and not result.score.passed


async def test_a_wrong_argument_fails_only_the_arguments_layer(tmp_path, readonly):
    result, _ = await _run(
        "get_baseline_resting_hr",
        [reply(tool_use_block("get_baseline", {"metric": "steps"})), reply(text_block("x"))],
        tmp_path,
        readonly,
    )
    assert (result.score.routing, result.score.arguments, result.score.execution) == (True, False, True)


async def test_integer_and_float_arguments_compare_by_value(tmp_path, readonly):
    arguments = {"date": TODAY.isoformat(), "steps": 8200.0}
    responses = [reply(tool_use_block("log_daily_metric", arguments)), reply(text_block("ok"))]
    result, _ = await _run("log_daily_metric_steps", responses, tmp_path, readonly)
    assert result.score.passed


async def test_a_tool_error_fails_execution(tmp_path, readonly):
    result, _ = await _run(
        "get_baseline_resting_hr",
        [reply(tool_use_block("get_baseline", {"metric": "bogus_metric"})), reply(text_block("x"))],
        tmp_path,
        readonly,
    )
    assert result.trace.tool_calls[0].is_error is True
    assert result.score.execution is False
    assert "tool_error" in {f["code"] for f in result.score.failures}


async def test_a_scenario_that_expects_the_refusal_accepts_it(tmp_path, readonly):
    scenario = Scenario(
        id="private_weight",
        tool="get_baseline",
        kind="permission",
        fixture="permission_limited",
        question="What's normal for my weight?",
        must_call=["get_baseline"],
        arguments={"metric": "weight_kg"},
        expect_error=True,
    )
    model = ScriptedModel(
        [reply(tool_use_block("get_baseline", {"metric": "weight_kg"})), reply(text_block("That metric is private."))]
    )
    result = await run_scenario(scenario, model, tmp_path, TODAY, readonly)
    assert result.trace.tool_calls[0].is_error is True
    assert result.score.passed


async def test_answering_without_any_tool_fails_routing(tmp_path, readonly):
    result, _ = await _run("get_baseline_resting_hr", [reply(text_block("It is probably 60."))], tmp_path, readonly)
    assert "no_tool_call" in {f["code"] for f in result.score.failures}
    assert result.trace.terminated == "end_turn" and not result.score.passed


async def test_parallel_tool_calls_are_all_executed_and_all_answered(tmp_path, readonly):
    blocks = [tool_use_block("get_data_status", {}, "t1"), tool_use_block("get_import_status", {}, "t2")]
    result, model = await _run("get_data_status_stale", [reply(*blocks), reply(text_block("ok"))], tmp_path, readonly)
    assert result.score.passed and result.trace.called_tools() == ["get_data_status", "get_import_status"]
    answered = model.calls[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in answered] == ["t1", "t2"]


async def test_a_loop_that_never_stops_is_cut_off(tmp_path):
    fixture = FIXTURES["empty"]
    build_fixture(fixture, tmp_path / "empty.db", TODAY)
    model = ScriptedModel([reply(tool_use_block("get_data_status", {}, f"t{i}")) for i in range(5)])
    with loaded_server(tmp_path / "empty.db") as server:
        async with Client(server.mcp) as client:
            trace = await run_agent(model, client, scenario_id="loop", question="?", system="s", max_turns=3)
    assert (trace.terminated, trace.turns, len(trace.tool_calls)) == ("max_turns", 3, 3)


# ---- the HTTP model client ---------------------------------------------------------------------------------


class _Response:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code, body="boom"):
    return urllib.error.HTTPError(API_URL, code, "err", {}, io.BytesIO(body.encode()))


def test_http_client_sends_the_documented_request():
    seen = []

    def urlopen(request, timeout):
        seen.append(request)
        return _Response(reply(text_block("hi")))

    client = AnthropicHTTP("sk-test", "some-model", urlopen=urlopen)
    response = client.complete("sys", [{"role": "user", "content": "q"}], [{"name": "t"}])
    assert response["content"][0]["text"] == "hi"
    (request,) = seen
    headers = {k.lower(): v for k, v in request.header_items()}
    assert request.full_url == API_URL and headers["x-api-key"] == "sk-test"
    assert headers["anthropic-version"] == "2023-06-01"
    body = json.loads(request.data)
    assert body["model"] == "some-model" and body["system"] == "sys" and body["tool_choice"] == {"type": "auto"}
    assert body["tools"] == [{"name": "t"}] and body["messages"] == [{"role": "user", "content": "q"}]


def test_http_client_retries_transient_errors_then_gives_up_on_permanent_ones():
    attempts, sleeps = [], []

    def flaky(request, timeout):
        attempts.append(1)
        if len(attempts) == 1:
            raise _http_error(429)
        return _Response(reply(text_block("ok")))

    client = AnthropicHTTP("k", "m", urlopen=flaky, sleep=sleeps.append)
    assert client.complete("s", [], [])["content"][0]["text"] == "ok"
    assert sleeps == [1]

    def rejected(request, timeout):
        raise _http_error(400, "bad request")

    with pytest.raises(ModelError, match="400"):
        AnthropicHTTP("k", "m", urlopen=rejected, sleep=sleeps.append).complete("s", [], [])

    def overloaded(request, timeout):
        raise _http_error(529)

    with pytest.raises(ModelError, match="529"):
        AnthropicHTTP("k", "m", urlopen=overloaded, sleep=sleeps.append).complete("s", [], [])


async def test_the_loop_works_end_to_end_over_the_http_client(tmp_path, readonly):
    requests = []
    answers = [
        reply(tool_use_block("get_baseline", {"metric": "resting_heart_rate"}, "toolu_abc")),
        reply(text_block("Your resting heart rate is steady.")),
    ]

    def urlopen(request, timeout):
        requests.append(json.loads(request.data))
        return _Response(answers.pop(0))

    result = await run_scenario(
        _scenario("get_baseline_resting_hr"), AnthropicHTTP("k", "m", urlopen=urlopen), tmp_path, TODAY, readonly
    )
    assert result.score.passed and result.trace.final_answer == "Your resting heart rate is steady."
    tool_result = requests[1]["messages"][-1]["content"][0]
    assert tool_result["tool_use_id"] == "toolu_abc" and json.loads(tool_result["content"])["baseline"]["n"] == 90
    assert requests[1]["messages"][1]["content"][0] == {
        "type": "tool_use",
        "id": "toolu_abc",
        "name": "get_baseline",
        "input": {"metric": "resting_heart_rate"},
    }


# ---- gates, summary and replayable artifacts ---------------------------------------------------------------


def _stub(scenario_id, **score):
    base = {"routing": True, "arguments": None, "execution": True}
    return SimpleNamespace(scenario=SimpleNamespace(id=scenario_id), score=Score(**{**base, **score}))


def test_scored_layers_passing_is_incomplete_while_answer_layers_are_not_scored():
    summary = summarize([_stub("a"), _stub("b")])
    assert summary["status"] == "INCOMPLETE"
    assert summary["gates"]["routing"]["status"] == "PASS" and summary["gates"]["routing"]["rate"] == 1.0
    assert summary["gates"]["arguments"]["status"] == "NOT_SCORED"
    assert summary["gates"]["interpretation"]["status"] == "NOT_SCORED"


def test_a_layer_below_its_threshold_fails_the_run():
    results = [_stub(str(i), routing=i >= 2) for i in range(10)]
    summary = summarize(results)
    assert summary["gates"]["routing"]["rate"] == 0.8 and summary["status"] == "FAIL"


def test_one_critical_failure_fails_the_run_even_when_every_rate_passes():
    results = [_stub(str(i)) for i in range(99)]
    results.append(_stub("bad", routing=False, critical=["tool_hallucination"]))
    summary = summarize(results)
    assert summary["gates"]["routing"]["status"] == "PASS"
    assert summary["status"] == "FAIL"
    assert summary["critical_failures"] == [{"scenario": "bad", "code": "tool_hallucination"}]


async def test_a_run_writes_replayable_artifacts(tmp_path, readonly):
    good, _ = await _run(
        "get_baseline_resting_hr",
        [reply(tool_use_block("get_baseline", {"metric": "resting_heart_rate"})), reply(text_block("done"))],
        tmp_path,
        readonly,
    )
    bad, _ = await _run("get_data_status_stale", [reply(text_block("no tools"))], tmp_path, readonly)
    out = tmp_path / "run"
    write_run(out, [good, bad], summarize([good, bad]))

    assert json.loads((out / "summary.json").read_text())["scenarios"] == 2
    assert len((out / "scenarios.jsonl").read_text().splitlines()) == 2
    (failure,) = [json.loads(line) for line in (out / "failures.jsonl").read_text().splitlines()]
    assert failure["scenario"] == "get_data_status_stale" and failure["failures"][0]["code"] == "no_tool_call"
    trace = json.loads((out / "traces" / "get_baseline_resting_hr.json").read_text())
    assert trace["tool_trace"][0]["tool"] == "get_baseline" and trace["final_answer"] == "done"
    assert trace["tool_scores"] == {"routing": True, "arguments": True, "execution": True}
    assert trace["answer_scores"] == {"interpretation": None, "grounding": None}


# ---- command line ------------------------------------------------------------------------------------------


def _cli(*args, **env):
    environment = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL")}
    environment.update(env)
    return subprocess.run(
        [sys.executable, "-m", "eval.agent", *args], cwd=REPO_ROOT, env=environment, capture_output=True, text=True
    )


def test_cli_coverage_strict_passes_when_every_tool_is_represented(readonly):
    done = _cli("--coverage", "--strict")
    assert done.returncode == 0, done.stderr
    assert f"{len(readonly)}/{len(readonly)} tools represented" in done.stdout


def test_cli_refuses_a_live_run_without_credentials():
    done = _cli()
    assert done.returncode == 2 and "ANTHROPIC_API_KEY" in done.stderr
