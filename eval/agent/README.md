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
    pytest -q tests/test_agent_permission_eval.py tests/test_permission_regressions.py   # permission boundary

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
| permission | no private value, private-only day or private metric turns up in a tool result, an exported file or the answer; private metrics are refused; public data stays available | scored (every scenario whose fixture configures private fields) |

Unscored layers are reported as `NOT_SCORED`, never as passed, and a run with
any unscored layer ends `INCOMPLETE`, not `PASS`. A critical failure
(`tool_hallucination`, `forbidden_tool_called`) fails the run whatever the
rates are. Gates: routing, arguments, interpretation and grounding >= 95%,
execution >= 98%, permission 100%. A permission leak (`private_value_exposed`, `private_date_exposed`,
`private_metric_not_refused`, `private_metric_in_analysis`, `private_value_in_answer`,
`private_date_in_answer`) is also a critical failure.

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

## Permission boundary

`permissions.py` scores whether `HEALTH_PRIVATE_FIELDS` held, with no model involved. The ground truth is the
fixture's own rows (the stored private values, and the days on which only a private metric has data), never a
second copy of the server's redaction, so what the server returns is checked against what was stored.

`BOUNDARY` is the evaluator's statement of how each tool is expected to treat a private metric:

| kind | expectation | tools |
| --- | --- | --- |
| `refuses` | a call naming a private metric returns an error, and the answer says so | `get_metric_provenance`, `get_metric_history`, `get_baseline`, `detect_metric_anomalies`, `calculate_metric_trend`, `compare_metric_periods`, `find_metric_correlation` |
| `redacts_values` | the field stays in the shape but is null; the write is kept, only the echo is redacted | `read_health_data`, `export_health_data_csv` (the file is scanned too), `read_workout_sessions`, `log_daily_metric`, `clear_metric`, `log_workout_session` |
| `redacts_rows` | raw rows are still listed with a null value; filtering on the private metric is refused | `read_measurements`, `log_measurement` |
| `excludes` | private metrics, and anything derived from them (correlated metrics, coverage dates), are left out | `aggregate_measurements`, `get_recent_changes`, `explain_metric_change`, `get_data_status`, `get_import_status` |

Every registered tool must be classified (a test fails otherwise). A tool that has no permission-sensitive
behavior would be `NOT_APPLICABLE` there, with a reason, and is then not a coverage gap; a sensitive tool with
no scenario is a gap (`NOT_SCORED`). Today no tool is NOT_APPLICABLE: each reads, derives from, aggregates or
writes data that the private-field setting covers. `python -m eval.agent --coverage` reports this on its own
lines, and `--strict` also fails on an uncovered or unclassified tool.

What is checked, on every tool result, on the CSV file `export_health_data_csv` writes, and on the final answer:

- a stored private value, in any spelling the server or a model might use (`private_value_exposed`,
  `private_value_in_answer`); a value the caller supplied to a write is allowed back only under `logged`
- a day that only has private data (`private_date_exposed`, `private_date_in_answer`)
- the private metric's name inside an analysis that did not ask about it, for example as a correlated metric
  (`private_metric_in_analysis`)
- a request that names a private metric and was answered instead of refused (`private_metric_not_refused`)
- `permission.public` entries (a tool and a path) that did not return data (`public_data_unavailable`), and a
  refusal the answer never mentions (`refusal_not_acknowledged`, not critical)

`scenarios/permission.yaml` has one scenario per tool (21: `read_measurements` has a filtered and an unfiltered
one) on `permission_limited`: `weight_kg` and `heart_rate` private, a weight series that moves exactly with
sleep (so a correlation would expose it), session heart rates, one day with only a weight reading, and a recorded
import span. Because the layer is scored for every scenario on a private fixture, any other scenario added on it
is a leak check as well.

`tests/test_agent_permission_eval.py` proves the evaluator fails: it runs every scenario against a server that
ignores `HEALTH_PRIVATE_FIELDS` and requires a critical permission failure for each tool that can leak, and
scores fabricated traces with one injected leak each.

## Scenarios and fixtures

`scenarios/*.yaml`: one `should_use` scenario and one `permission` scenario per tool. The evaluator, not
the model, owns the expectations. Date placeholders (`{today}`, `{yesterday}`,
`{d3}`, `{d7}`, `{d14}`, `{d30}`, `{d120}`) are resolved at run time.

`fixtures.py`: deterministic datasets defined relative to today, because the
server reads the real clock: `complete`, `incomplete`, `sparse`, `stale`,
`empty`, `conflicting`, `pagination`, `permission_limited` (private weight and heart rate, see above)
and `import_failed`. `tests/test_agent_eval.py` checks that each one produces
the state it promises.

The coverage matrix is built from the live tool list. A tool counts only for
scenarios that declare it as their own `tool`; being called inside another
scenario earns no coverage, and a newly registered tool appears as a gap.

## Not built yet

`argument` scenarios, interpretation and grounding scenarios beyond
`scenarios/answers.yaml` (seven so far, for six tools); multi-tool and adversarial scenarios; the
optional semantic judge; the CI gate. The coverage report lists these gaps.

### Remaining permission gaps

- Not run against a live model: the layer is proven only with a scripted model, so whether a real model
  repeats, infers or invents a private value is unmeasured. Only numbers and days are matched in an answer; a
  vague restatement ("your weight is stable") is not.
- `read_measurements` and `log_measurement` list private rows with their timestamps (value null). That is the
  documented "reading-timestamp visibility" decision, so the layer does not score it as a leak.
- `clear_metric` on a day with only private data answers differently from a day with none, which tells the
  caller whether a private reading existed on a date they named. Not scored.
- Truth covers metric rows and session heart rate. Other private session fields, notes and free text are not
  modeled, and `workout_minutes` as a private field is not exercised by any fixture.
- Matching is on spelling: an arithmetic derivation of a private value (a difference, a mean) is not caught.
- Single-turn and single-fixture: no multi-tool chaining (a model joining two public results to infer a private
  one), no prompt-injection or "ignore the privacy setting" adversarial scenarios, no MCP resources.
- The classification table is the evaluator's own; it is checked against the registered tool list, not against
  the production privacy code, which is deliberate (no duplicated logic) but means a wrong expectation there
  is caught only by review.
