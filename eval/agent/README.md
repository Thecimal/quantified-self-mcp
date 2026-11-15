# Agent-loop eval

Runs a real model against the real MCP server in a multi-turn tool loop and
scores what happened. It complements `eval/tool_routing`, which only checks the
first tool the model picks.

    question -> model -> real tool call -> real server -> real result -> model -> ... -> final answer

The server is the production module (`server.py`) loaded in-process on a
fixture database, so the model sees exactly the registered schemas and gets
exactly the answers a client would.

## Run it

Offline checks (no key, also run by `pytest`):

    python -m eval.agent --coverage --strict     # every registered tool must have a scenario
    pytest -q tests/test_agent_eval.py           # harness, fixtures, scoring, HTTP client, artifacts
    pytest -q tests/test_agent_answer_eval.py    # final-answer scoring: interpretation and grounding

Live run (costs tokens):

    export ANTHROPIC_API_KEY=sk-ant-...
    python -m eval.agent --model <model-id>                 # all scenarios
    python -m eval.agent --model <model-id> --id get_baseline_resting_hr

There is no default model on purpose: ids change, and a stale default would
silently test the wrong one. Artifacts go to `eval/agent/results/run-<utc>/`
(git-ignored): `summary.json`, `scenarios.jsonl`, `failures.jsonl` and one full
trace per scenario in `traces/`.

## What is scored today

| layer | how | status |
| --- | --- | --- |
| routing | required tools called, none forbidden, none outside the allowed set, no unregistered tool | scored |
| arguments | the scenario's expected arguments appear on a call to its tool | scored (when a scenario pins any) |
| execution | every call is registered and succeeds (unless the scenario expects the refusal) and the loop finishes | scored |
| interpretation | the answer respects `data_health` and the claim tier, and states no cause for an association | scored (scenarios with an `answer` spec) |
| grounding | the answer's numbers, units, dates and metric match the tool results; missing data is not a value | scored (scenarios with an `answer` spec) |

Unscored layers are reported as `NOT_SCORED`, never as passed, and a run with
any unscored layer ends `INCOMPLETE`, not `PASS`. A critical failure
(`tool_hallucination`, `forbidden_tool_called`) fails the run whatever the
rates are. Gates: routing, arguments, interpretation and grounding >= 95%,
execution >= 98%.

## Scoring the final answer

`answers.py` scores the trace's final answer with no model involved, so it runs offline and gives the same
verdict every time. A scenario opts in with an `answer:` spec; scenarios without one keep both layers
NOT_SCORED, and a layer is scored only when at least one of its checks applied.

Checks that follow from the tool results themselves (no per-scenario typing):

- `data_health.status` (or `get_data_status`'s own status) of STALE, MIXED_SOURCES, IMPORT_INCOMPLETE or
  INSUFFICIENT_DATA must be disclosed (`stale_data_not_disclosed`, `source_conflict_omitted`,
  `import_failure_omitted`, `insufficient_data_not_acknowledged`), and the answer must not call such data
  complete or up to date (`contradicts_data_health`).
- claim tier `suggestive` needs hedged wording (`suggestive_presented_as_supported`); tiers below `supported`
  forbid certainty language (`overconfident_conclusion`).
- an association (`find_metric_correlation`) must not be given a cause (`causal_claim_unsupported`).
- a window with no observations must not be answered with a zero (`missing_data_as_zero`).

Checks the scenario pins (`answer.values`, `answer.dates`, `answer.require`, `answer.forbid`): a number read
from the result at a path must appear with its unit (`value_not_stated`, `wrong_unit`), a date must appear
(`date_not_stated`), plus scenario-specific required or forbidden phrases under their own code, such as
`wrong_metric`. Expected numbers are read from the real tool result, never typed into the scenario. A failure
is `{layer, code, detail}` and the detail names the violated expectation and the offending sentence.

Limits: the checks are phrase-based and conservative. They catch a missing mandatory caveat or an unsupported
claim; they do not judge style, and a caveat worded in a way none of the patterns recognize fails. Only the
tiers and statuses above are enforced (a claim's `must_state` codes are not checked one by one), a hedge is
required in the answer as a whole rather than per claim, and a negation within a few words turns a sentence
into a disclaimer. There is no model judge yet.

## Scenarios and fixtures

`scenarios/*.yaml`: one `should_use` scenario per tool so far. The evaluator, not
the model, owns the expectations. Date placeholders (`{today}`, `{yesterday}`,
`{d3}`, `{d7}`, `{d14}`, `{d30}`) are resolved at run time.

`fixtures.py`: deterministic datasets defined relative to today, because the
server reads the real clock: `complete`, `incomplete`, `sparse`, `stale`,
`empty`, `conflicting`, `pagination`, `permission_limited` (a private metric)
and `import_failed`. `tests/test_agent_eval.py` checks that each one produces
the state it promises.

The coverage matrix is built from the live tool list. A tool counts only for
scenarios that declare it as their own `tool`; being called inside another
scenario earns no coverage, and a newly registered tool appears as a gap.

## Not built yet

`argument` and `permission` scenarios, interpretation and grounding scenarios beyond
`scenarios/answers.yaml` (seven so far, for six tools); multi-tool and adversarial scenarios; the
optional semantic judge; the CI gate. The coverage report lists these gaps.
