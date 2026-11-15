"""
Offline tests for final-answer scoring (eval/agent/answers.py): the interpretation and grounding layers.

No model and no key: a ScriptedModel makes the tool call and then gives a prepared final answer, while the MCP server,
the fixtures and the scoring are real. Every shipped answer scenario is run with one correct answer (it must pass every
layer it scores) and with answers that go wrong in a specific way (each must fail with that specific code).
"""

import asyncio
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from eval.agent.answers import score_answer, validate_spec  # noqa: E402
from eval.agent.loop import ToolCall, Trace  # noqa: E402
from eval.agent.model import ScriptedModel, reply, text_block, tool_use_block  # noqa: E402
from eval.agent.report import format_summary, summarize, write_run  # noqa: E402
from eval.agent.runner import live_tool_info, run_scenario  # noqa: E402
from eval.agent.scenarios import Scenario, load_scenarios, validate_scenarios  # noqa: E402

TODAY = date.today()
_LATEST = TODAY - timedelta(days=12)  # the `stale` fixture ends 12 days ago
LATEST = f"{_LATEST.strftime('%b')} {_LATEST.day}"


@pytest.fixture(scope="module")
def readonly():
    return asyncio.run(live_tool_info())


def _scenario(scenario_id):
    return next(s for s in load_scenarios() if s.id == scenario_id)


# scenario id -> (the tool call the model makes, a correct answer)
CALLS = {
    "answer_hrv_average_wrong_metric": (
        ("get_baseline", {"metric": "hrv_ms"}),
        "Your HRV has averaged about 52.8 ms over the last three months, with a few days missing from the window.",
    ),
    "answer_sleep_hrv_suggestive_as_supported": (
        ("find_metric_correlation", {"metric_a": "sleep_hours", "metric_b": "hrv_ms"}),
        "Sleep and HRV move together in your data (r = 0.89 over 90 days). That is an association: it suggests more "
        "sleep may go with higher HRV, but it does not show that sleep causes it. Some days are missing.",
    ),
    "answer_sparse_baseline_confident": (
        ("get_baseline", {"metric": "hrv_ms"}),
        "I can't give you a reliable baseline yet: there are only 5 HRV readings in the last 90 days. Their rough "
        "average is 49.4 ms, but that isn't enough to call it your normal.",
    ),
    "answer_conflicting_sources_omitted": (
        ("get_metric_history", {"metric": "steps"}),
        "Your steps come from two sources, Apple Watch and Garmin, and they disagree by roughly 900 steps a day, so I "
        "can't give you one reliable figure.",
    ),
    "answer_missing_steps_as_zero": (
        ("get_metric_history", {"metric": "steps"}),
        "There is no step data recorded for yesterday, so I can't say how many steps you took.",
    ),
    "answer_stale_baseline_with_caveats": (
        ("get_baseline", {"metric": "steps"}),
        f"Your average was about 7,805 steps a day, but the most recent data is from {LATEST} (12 days old), so it may "
        "not reflect how you are doing now. The evidence is only suggestive.",
    ),
    "answer_failed_import_presented_current": (
        ("get_data_status", {}),
        "I can't confirm that: your latest import failed, so the newest days may be missing.",
    ),
}

# (scenario id, a wrong answer, the layer and code the scorer must report)
BAD_ANSWERS = [
    # correct tool, wrong reading of the result: another metric and another unit
    ("answer_hrv_average_wrong_metric", "Your resting heart rate has averaged 52.8 bpm.", "grounding", "wrong_metric"),
    ("answer_hrv_average_wrong_metric", "Your HRV has averaged 52.8 bpm.", "grounding", "wrong_metric"),
    (
        "answer_hrv_average_wrong_metric",
        "Your HRV has averaged about 52.8 over three months.",
        "grounding",
        "wrong_unit",
    ),
    (
        "answer_hrv_average_wrong_metric",
        "Your HRV has averaged 61 ms over three months.",
        "grounding",
        "value_not_stated",
    ),
    # suggestive evidence presented as supported
    (
        "answer_sleep_hrv_suggestive_as_supported",
        "Yes, more sleep clearly raises your HRV (r = 0.886). The data proves it.",
        "interpretation",
        "suggestive_presented_as_supported",
    ),
    (
        "answer_sleep_hrv_suggestive_as_supported",
        "Yes, more sleep clearly raises your HRV (r = 0.886). The data proves it.",
        "interpretation",
        "overconfident_conclusion",
    ),
    (
        "answer_sleep_hrv_suggestive_as_supported",
        "Sleep may be linked to HRV (r = 0.89), because sleep drives recovery.",
        "interpretation",
        "causal_claim_unsupported",
    ),
    # insufficient data presented as a confident conclusion
    (
        "answer_sparse_baseline_confident",
        "Your normal HRV is 49.4 ms.",
        "interpretation",
        "insufficient_data_not_acknowledged",
    ),
    # a source conflict left out, or contradicted
    (
        "answer_conflicting_sources_omitted",
        "You've been averaging about 6,900 steps a day.",
        "interpretation",
        "source_conflict_omitted",
    ),
    (
        "answer_conflicting_sources_omitted",
        "All your data is complete and up to date, averaging 6,900 steps a day.",
        "interpretation",
        "contradicts_data_health",
    ),
    # missing data read as zero
    ("answer_missing_steps_as_zero", "You took 0 steps yesterday.", "grounding", "missing_data_as_zero"),
    (
        "answer_missing_steps_as_zero",
        "You didn't walk at all yesterday: zero steps.",
        "grounding",
        "missing_data_as_zero",
    ),
    (
        "answer_missing_steps_as_zero",
        "You took 0 steps yesterday.",
        "interpretation",
        "insufficient_data_not_acknowledged",
    ),
    # stale data without its age, and without the hedge its claim tier requires
    (
        "answer_stale_baseline_with_caveats",
        "Your average is 7,805 steps a day.",
        "interpretation",
        "stale_data_not_disclosed",
    ),
    (
        "answer_stale_baseline_with_caveats",
        "Your average is 7,805 steps a day.",
        "interpretation",
        "suggestive_presented_as_supported",
    ),
    ("answer_stale_baseline_with_caveats", "Your average is 7,805 steps a day.", "grounding", "date_not_stated"),
    (
        "answer_stale_baseline_with_caveats",
        "Your average is 7,805 steps a day, and your data is fully up to date.",
        "interpretation",
        "contradicts_data_health",
    ),
    # a failed import presented as current
    (
        "answer_failed_import_presented_current",
        "Yes, your data is up to date through today.",
        "interpretation",
        "import_failure_omitted",
    ),
    (
        "answer_failed_import_presented_current",
        "Yes, your data is up to date through today.",
        "interpretation",
        "contradicts_data_health",
    ),
]


async def _run(scenario_id, final_answer, tmp_path, readonly):
    (tool, arguments), _ = CALLS[scenario_id]
    model = ScriptedModel([reply(tool_use_block(tool, arguments)), reply(text_block(final_answer))])
    return await run_scenario(_scenario(scenario_id), model, tmp_path, TODAY, readonly)


def _codes(result, layer=None):
    return [f["code"] for f in result.score.failures if layer is None or f["layer"] == layer]


# ---- the shipped scenarios ------------------------------------------------------------------------------------


def test_every_shipped_answer_scenario_has_a_correct_and_a_wrong_scripted_answer():
    shipped = {s.id for s in load_scenarios() if s.answer}
    assert shipped == set(CALLS)
    assert shipped == {scenario_id for scenario_id, *_ in BAD_ANSWERS}
    assert len(shipped) >= 6


def test_answer_scenarios_validate_against_the_live_tool_list(readonly):
    assert validate_scenarios(load_scenarios(), set(readonly)) == []


@pytest.mark.parametrize("scenario_id", sorted(CALLS))
async def test_a_correct_answer_with_its_caveats_passes_every_scored_layer(scenario_id, tmp_path, readonly):
    result = await _run(scenario_id, CALLS[scenario_id][1], tmp_path, readonly)
    assert result.score.failures == []
    assert result.score.passed
    assert result.score.interpretation is not False and result.score.grounding is not False
    assert (result.score.interpretation, result.score.grounding) != (None, None)  # something was actually checked


@pytest.mark.parametrize(("scenario_id", "answer", "layer", "code"), BAD_ANSWERS)
async def test_a_wrong_answer_fails_with_the_violated_expectation(scenario_id, answer, layer, code, tmp_path, readonly):
    result = await _run(scenario_id, answer, tmp_path, readonly)
    assert code in _codes(result, layer), result.score.failures
    assert result.score.layers()[layer] is False
    assert not result.score.passed
    # the tool layers are not touched: the right tool ran and succeeded
    assert (result.score.routing, result.score.execution) == (True, True)
    detail = next(f["detail"] for f in result.score.failures if f["code"] == code)
    assert detail and len(detail) > 20


async def test_correct_tool_execution_is_not_enough(tmp_path, readonly):
    # Routing, arguments and execution all pass; only the answer layer fails.
    result = await _run(
        "answer_hrv_average_wrong_metric", "Your resting heart rate has averaged 52.8 bpm.", tmp_path, readonly
    )
    assert (result.score.routing, result.score.arguments, result.score.execution) == (True, True, True)
    assert result.score.grounding is False
    assert not result.score.passed


async def test_scenarios_without_an_answer_spec_stay_unscored(tmp_path, readonly):
    scenario = next(s for s in load_scenarios() if s.id == "get_baseline_resting_hr")
    assert not scenario.answer
    model = ScriptedModel(
        [
            reply(tool_use_block("get_baseline", {"metric": "resting_heart_rate"})),
            reply(text_block("Your resting heart rate is 59.")),
        ]
    )
    result = await run_scenario(scenario, model, tmp_path, TODAY, readonly)
    assert (result.score.interpretation, result.score.grounding) == (None, None)


# ---- the report -----------------------------------------------------------------------------------------------


async def test_answer_results_appear_in_the_existing_report(tmp_path, readonly):
    good = await _run(
        "answer_sparse_baseline_confident", CALLS["answer_sparse_baseline_confident"][1], tmp_path, readonly
    )
    bad = await _run("answer_missing_steps_as_zero", "You took 0 steps yesterday.", tmp_path, readonly)
    summary = summarize([good, bad])

    assert summary["gates"]["interpretation"]["scored"] == 2
    assert summary["gates"]["grounding"]["scored"] == 2  # the sparse scenario pins a value; the empty one checks zero
    assert summary["gates"]["interpretation"]["status"] == "FAIL"  # 1 of 2 answers is right
    assert "interpretation" in format_summary(summary)

    out = tmp_path / "run"
    write_run(out, [good, bad], summary)
    failed = [json.loads(line) for line in (out / "failures.jsonl").read_text().splitlines()]
    assert [f["scenario"] for f in failed] == ["answer_missing_steps_as_zero"]
    assert {"missing_data_as_zero", "insufficient_data_not_acknowledged"} <= {x["code"] for x in failed[0]["failures"]}
    trace = json.loads((out / "traces" / "answer_missing_steps_as_zero.json").read_text())
    assert trace["answer_scores"] == {"interpretation": False, "grounding": False}


# ---- the validators on their own ------------------------------------------------------------------------------


def _trace(answer, *calls):
    trace = Trace(scenario="t", model="m", prompt="q", system="s", final_answer=answer, terminated="end_turn")
    trace.tool_calls = [ToolCall(tool, {}, False, True, result) for tool, result in calls]
    return trace


BASELINE = {
    "baseline": {"mean": 7805.06},
    "data_health": {"status": "VALID", "freshness": {"latest_data": "2026-11-03"}},
}
VALUE_SPEC = {"values": [{"tool": "get_baseline", "path": "baseline.mean", "unit": r"\bsteps?\b"}]}


@pytest.mark.parametrize(
    "answer",
    [
        "about 7,805 steps",
        "7805 steps a day",
        "roughly 7,805.1 steps",
        "7.8k steps a day is wrong but 7,805 steps is right",
    ],
)
def test_numbers_match_by_value_whatever_the_formatting(answer):
    assert score_answer(VALUE_SPEC, _trace(answer, ("get_baseline", BASELINE)), TODAY).grounding is True


def test_the_answers_own_rounding_sets_the_tolerance():
    spec = {"values": [{"tool": "t", "path": "x"}]}
    for text, ok in (("52.8", True), ("53", True), ("52.9", False), ("52", False)):
        verdict = score_answer(spec, _trace(text, ("t", {"x": 52.82})), TODAY).grounding
        assert verdict is ok, text


def test_a_value_missing_from_the_result_is_reported_not_passed():
    score = score_answer(VALUE_SPEC, _trace("7,805 steps", ("get_baseline", {"baseline": {}})), TODAY)
    assert score.grounding is False
    assert score.failures[0]["code"] == "value_missing_from_results"


@pytest.mark.parametrize("text", ["Nov 3", "November 3", "3 November", "2026-11-03", "on Nov. 3rd"])
def test_dates_are_recognised_in_common_forms(text):
    spec = {"dates": [{"tool": "get_baseline", "path": "data_health.freshness.latest_data"}]}
    verdict = score_answer(
        spec, _trace(f"The latest data is from {text}.", ("get_baseline", BASELINE)), TODAY
    ).grounding
    assert verdict is True


def test_a_similar_looking_date_is_not_the_date():
    spec = {"dates": ["2026-11-03"]}
    assert score_answer(spec, _trace("Data from Nov 30.", ("t", {})), TODAY).grounding is False
    assert score_answer(spec, _trace("Data from Nov 13.", ("t", {})), TODAY).grounding is False


def test_a_disclaimer_is_not_a_causal_claim_but_a_claim_is():
    result = {"r": 0.5, "metric_a": "a", "metric_b": "b", "claim": {"decision": {"tier": "supported"}}}
    ok = "They move together, but correlation does not mean that one causes the other."
    bad = "They move together because one causes the other."
    assert score_answer({"forbid_causal": True}, _trace(ok, ("c", result)), TODAY).interpretation is True
    assert score_answer({"forbid_causal": True}, _trace(bad, ("c", result)), TODAY).interpretation is False


def test_certainty_words_are_allowed_when_the_evidence_is_supported():
    result = {"claim": {"decision": {"tier": "supported"}}, "data_health": {"status": "VALID"}}
    spec = {"forbid_causal": True}
    assert score_answer(spec, _trace("Your baseline is clearly 52 ms.", ("b", result)), TODAY).interpretation is True
    suggestive = {"claim": {"decision": {"tier": "suggestive"}}, "data_health": {"status": "VALID"}}
    verdict = score_answer(spec, _trace("It clearly shows a rise, and it may continue.", ("b", suggestive)), TODAY)
    assert "overconfident_conclusion" in [f["code"] for f in verdict.failures]


def test_negated_clean_claims_and_zero_are_not_flagged():
    stale = {"data_health": {"status": "STALE", "freshness": {"latest_data": "2026-11-03"}}}
    ok = score_answer(
        {"forbid_causal": True}, _trace("The data is not up to date: it is 12 days old.", ("t", stale)), TODAY
    )
    assert ok.interpretation is True
    empty = {"points": [], "data_health": {"status": "INSUFFICIENT_DATA", "reasons": ["no_observations_in_window"]}}
    ok = score_answer(
        {"forbid_causal": True}, _trace("There is no data, which is not the same as zero steps.", ("t", empty)), TODAY
    )
    assert (ok.interpretation, ok.grounding) == (True, True)


def test_nothing_is_passed_that_was_not_checked():
    clean = {"data_health": {"status": "VALID"}, "claim": {"decision": {"tier": "supported"}}}
    assert score_answer({"forbid_causal": True}, _trace("All good.", ("t", clean)), TODAY).grounding is None
    assert score_answer({}, _trace("anything", ("t", clean)), TODAY).interpretation is None


def test_an_empty_final_answer_fails_instead_of_being_skipped():
    trace = _trace("", ("t", {}))
    trace.terminated = "max_turns"
    score = score_answer({"forbid_causal": True}, trace, TODAY)
    assert score.grounding is False and score.failures[0]["code"] == "no_final_answer"


def test_scenario_specific_require_and_forbid_name_their_own_code():
    spec = {
        "require": [{"code": "states_window", "layer": "grounding", "any": ["last (7|seven) days"]}],
        "forbid": [
            {"code": "diagnoses", "layer": "interpretation", "any": ["you have (\\w+ ){1,2}(disease|disorder)"]}
        ],
    }
    good = score_answer(spec, _trace("Over the last 7 days it was flat.", ("t", {})), TODAY)
    assert (good.interpretation, good.grounding) == (True, True)
    bad = score_answer(spec, _trace("You have a sleep disorder.", ("t", {})), TODAY)
    assert sorted(f["code"] for f in bad.failures) == ["diagnoses", "states_window"]


def test_answer_specs_are_validated():
    assert validate_spec({"values": [{"tool": "t", "path": "x"}], "forbid_causal": True}) == []
    problems = validate_spec(
        {
            "valus": [],
            "values": [{"tool": "t"}],
            "require": [{"code": "c", "layer": "routing", "any": ["x"]}],
            "forbid": [{"code": "c", "layer": "grounding", "any": ["("]}],
        }
    )
    assert len(problems) == 4
    assert any("unknown answer key 'valus'" in p for p in problems)
    assert any("layer must be one of" in p for p in problems)
    assert any("bad regex" in p for p in problems)


def test_an_interpretation_scenario_without_an_answer_spec_is_invalid():
    scenario = Scenario(
        id="x", tool="get_baseline", kind="interpretation", fixture="complete", question="q", must_call=["get_baseline"]
    )
    assert any("needs an answer spec" in p for p in validate_scenarios([scenario]))
