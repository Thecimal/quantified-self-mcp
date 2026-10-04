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
| interpretation | does the answer respect `data_health` / claim tier | **not scored yet** |
| final answer | numbers, dates, units, causal claims, leakage | **not scored yet** |

Unscored layers are reported as `NOT_SCORED`, never as passed, and a run with
any unscored layer ends `INCOMPLETE`, not `PASS`. A critical failure
(`tool_hallucination`, `forbidden_tool_called`) fails the run whatever the
rates are. Gates: routing, arguments, interpretation and grounding >= 95%,
execution >= 98%.

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

`should_not_use`, argument, permission, interpretation and grounding scenarios;
multi-tool and adversarial scenarios; the deterministic answer validators and
the semantic judge; the CI gate. The coverage report lists these gaps.
