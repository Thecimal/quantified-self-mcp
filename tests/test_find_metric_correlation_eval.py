"""
find_metric_correlation: can a client pick it correctly and report what it returns without overstating it?

A deterministic check of the contract a model works from: the tool's own description (what it says about choosing
between tools and about wording a result), its output (what is an observation, what is an association, what a claim
may not say) and the docs that describe its parameters. No model is called, so how a model actually behaves with
this contract is NOT verified here; that needs `python -m eval.agent --model <id>` (see eval/agent/README.md), whose
interpretation and final-answer layers are not scored yet. The scenario is the shipped one: the `complete` fixture
and the question "Does my sleep affect my HRV?", plus the `sparse` fixture as the thin-sample counterpart.
"""

import json
import math
import re
import sys
from datetime import date
from pathlib import Path

from fastmcp import Client

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from eval.agent.fixtures import FIXTURES, build_fixture  # noqa: E402
from eval.agent.runner import loaded_server  # noqa: E402

TOOL = "find_metric_correlation"
TODAY = date.today()


async def _tools(tmp_path):
    with loaded_server(tmp_path / "health.db") as server:
        async with Client(server.mcp) as client:
            return {t.name: t for t in await client.list_tools()}


async def _correlate(tmp_path, fixture_name, **arguments):
    db = tmp_path / f"{fixture_name}.db"
    build_fixture(FIXTURES[fixture_name], db, TODAY)
    with loaded_server(db) as server:
        async with Client(server.mcp) as client:
            return (await client.call_tool(TOOL, arguments)).structured_content


def _pearson(fixture_name, metric_a, metric_b):
    """Pearson r from the fixture's raw rows, by the textbook definition (one reading per metric per day)."""
    by_metric: dict[str, dict[str, float]] = {}
    for timestamp, metric, value, _source in FIXTURES[fixture_name].rows(TODAY):
        by_metric.setdefault(metric, {})[timestamp[:10]] = value
    days = sorted(set(by_metric[metric_a]) & set(by_metric[metric_b]))
    xs, ys = [by_metric[metric_a][d] for d in days], [by_metric[metric_b][d] for d in days]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    return cov / math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys)), len(days)


async def test_description_sends_neighbouring_questions_to_the_tools_that_answer_them(tmp_path):
    tools = await _tools(tmp_path)
    description = tools[TOOL].description or ""
    # Questions that look like a correlation question but belong to a sibling must be pointed at it by name,
    # and each named tool must exist in the live tool list.
    for sibling in ("explain_metric_change", "compare_metric_periods"):
        assert sibling in tools, f"{sibling} is no longer a registered tool"
        assert f"`{sibling}`" in description, f"the description never points a model at {sibling}"
    assert "different" in description  # it already says it needs two different metrics


async def test_description_says_how_to_word_a_causal_question_and_fill_the_decision_template(tmp_path):
    # The client sees only the docstring body: Args go to the input schema and Returns is dropped, so what a model
    # must know to read a result has to be in the body.
    description = (await _tools(tmp_path))[TOOL].description or ""
    assert "claim.decision" in description, "a model is never told to read claim.decision before reporting r"
    sc = await _correlate(tmp_path, "complete", metric_a="sleep_hours", metric_b="hrv_ms")
    template = sc["claim"]["decision"]["template"]

    # A placeholder the response hands to the client must be explained where the client reads how to use the tool.
    for placeholder in re.findall(r"\{(\w+)\}", template):
        assert placeholder in description, f"template placeholder {{{placeholder}}} is never explained"
    # must_state entries are codes; the description has to say they are put into plain words, not quoted.
    assert "plain words" in description
    # "Does my sleep affect my HRV?" is a causal question; the answer is an association and must say no more.
    assert "does not show" in description and "cause" in description


async def test_response_separates_observations_from_the_association_claim_and_caps_what_may_be_said(tmp_path):
    # --- complete data, a strong correlation: the best case a correlation can be in ---
    best = await _correlate(tmp_path, "complete", metric_a="sleep_hours", metric_b="hrv_ms")
    expected_r, expected_n = _pearson("complete", "sleep_hours", "hrv_ms")
    assert expected_n == 90
    # observations: what was measured over which days
    assert (best["r"], best["n"], best["lag_days"]) == (round(expected_r, 3), 90, 0)
    assert best["claim"]["evidence_a"]["observed_days"] == best["claim"]["evidence_b"]["observed_days"] == 90
    assert (
        best["claim"]["evidence_a"]["expected_days"] == 91
    )  # the default window is the 90 days up to today, inclusive
    # association: even with r this strong and 90 of 91 days present, the claim stops at "suggestive"
    # (practical significance is not evaluated), so "supported" wording is unreachable for a correlation
    decision = best["claim"]["decision"]
    assert abs(best["r"]) > 0.8
    assert decision["tier"] == decision["permitted_phrasing_class"] == "suggestive"
    assert decision["must_state"] == ["practical_not_evaluated"]
    # unsupported: nothing in the result asserts a cause
    assert "caus" not in json.dumps(best).lower()

    # --- five paired days: r is still computed, but the claim refuses to let it be reported as a finding ---
    thin = await _correlate(tmp_path, "sparse", metric_a="steps", metric_b="hrv_ms")
    thin_r, thin_n = _pearson("sparse", "steps", "hrv_ms")
    assert thin_n == 5
    assert (thin["r"], thin["n"]) == (round(thin_r, 3), 5)
    thin_decision = thin["claim"]["decision"]
    assert thin_decision["tier"] == "insufficient"
    assert "n_paired_below_minimum" in thin_decision["must_state"]
    assert thin_decision["template"] == "There isn't enough data to say."
    assert thin["data_health"]["status"] == "INSUFFICIENT_DATA"


async def test_docs_list_the_parameters_the_tool_really_takes(tmp_path):
    live = set((await _tools(tmp_path))[TOOL].input_schema["properties"])
    docs = (REPO_ROOT / "docs" / "tools.md").read_text(encoding="utf-8")
    entry = re.search(rf"\*\*`{TOOL}\(([^)]*)\)`\*\*", docs)
    assert entry, f"docs/tools.md has no entry for {TOOL}"
    documented = {part.split("=")[0].strip() for part in entry.group(1).split(",")}
    assert documented == live, f"docs list {sorted(documented)}, the tool takes {sorted(live)}"
    assert "90 days" in docs[entry.end() : entry.end() + 600]
