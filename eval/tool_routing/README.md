# Tool-routing eval

Tests whether an LLM picks the right tool out of the server's real,
currently-registered tool list -- not whether the tools work. Run this
after adding a tool, renaming one, or editing a docstring, to see whether
the change made routing better or worse.

It does **not** execute a full agent loop and does not require a
populated database: it sends each prompt once, with the live tool list
attached, and records which tool (if any) the model chose. That's enough
to catch the two failure modes that matter here: two tools with
overlapping descriptions, and a tool description that's too vague for
the model to realize it applies.

## Setup

```
python3 -m venv .venv
. .venv/bin/activate
pip install -r eval/tool_routing/requirements-eval.txt
export ANTHROPIC_API_KEY=sk-ant-...
```

## First run: check the tool names in prompts.yaml actually match your server

`prompts.yaml` was written from the tool names described in project notes,
not by reading your source. Confirm them against the live server before
trusting any score:

```
python eval/tool_routing/run_eval.py --dump-tools \
  --server-cmd "python -m quantified_self_mcp.server"
```

Fix any mismatch in `eval/tool_routing/prompts.yaml` (the `canonical` /
`acceptable` fields). The harness also prints a warning at eval time for
any referenced name it can't find on the live server, so a stale name
won't fail silently.

## Running the eval

```
python eval/tool_routing/run_eval.py \
  --server-cmd "python -m quantified_self_mcp.server" \
  --prompts eval/tool_routing/prompts.yaml \
  --out eval/tool_routing/results.json
```

Useful flags:

- `--category layer_boundary` -- run just one category while iterating on
  a specific pair of tool descriptions
- `--limit 10` -- quick smoke test
- `--model claude-sonnet-5` -- override the model under test (defaults to
  `$ANTHROPIC_MODEL` or `claude-sonnet-5`)

## Reading the report

Each prompt gets one verdict:

- `canonical` -- picked the tool a human reviewer would pick first
- `acceptable` -- picked a defensible alternative, not the tightest fit
- `wrong` -- picked a tool that doesn't answer the question well
- `no_tool` -- answered in prose without calling anything (usually means
  the model didn't realize the answer requires the database)
- `multi_tool_acceptable` / `multi_tool_wrong` -- called more than one
  tool in a single turn; acceptable only if every tool called is in the
  case's canonical/acceptable set

`canonical`, `acceptable`, and `multi_tool_acceptable` count as a pass.
The printed report breaks pass rate down by category, so a low score on
`layer_boundary` versus `causal_adversarial` points at a different fix:

- Whole category failing → the tool descriptions for that pair are too
  similar; tighten them or merge the tools.
- Scattered individual failures → probably fine, that's expected noise;
  re-run to see if it's stable before changing anything.

Results are also written to `--out` as JSON for tracking pass rate over
time as the tool surface changes.

## Extending the prompt set

Add entries to `prompts.yaml`. Keep `canonical` to the single best tool
and use `acceptable` generously for anything a human wouldn't flag as
wrong -- the goal is to catch real confusion, not to penalize reasonable
judgment calls the tool surface doesn't actually disambiguate.
