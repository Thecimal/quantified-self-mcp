"""python -m eval.agent [--coverage [--strict]] [--model MODEL] [--id ID ...] [--limit N] [--out DIR]"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from eval.agent.coverage import coverage_matrix, format_matrix
from eval.agent.model import AnthropicHTTP
from eval.agent.report import format_summary, summarize, write_run
from eval.agent.runner import live_tool_info, run_all
from eval.agent.scenarios import SCENARIO_DIR, load_scenarios, validate_scenarios

RESULTS_DIR = Path(__file__).parent / "results"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m eval.agent", description=__doc__)
    ap.add_argument("--scenarios", type=Path, default=SCENARIO_DIR)
    ap.add_argument("--coverage", action="store_true", help="print the tool coverage matrix and exit (no model needed)")
    ap.add_argument("--strict", action="store_true", help="with --coverage: exit 1 if any tool has no scenario")
    ap.add_argument("--model", default=os.environ.get("ANTHROPIC_MODEL"), help="model id (or $ANTHROPIC_MODEL)")
    ap.add_argument("--id", action="append", dest="ids", help="run only this scenario id (repeatable)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", type=Path, default=None, help="output directory (default: eval/agent/results/run-<utc>)")
    args = ap.parse_args(argv)

    scenarios = load_scenarios(args.scenarios)
    readonly = asyncio.run(live_tool_info())
    problems = validate_scenarios(scenarios, set(readonly))
    if problems:
        print("invalid scenarios:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 2
    matrix = coverage_matrix(set(readonly), scenarios)

    if args.coverage:
        print(format_matrix(matrix))
        return 1 if args.strict and matrix["gaps"]["represented"] else 0

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key or not args.model:
        print("ANTHROPIC_API_KEY and --model (or $ANTHROPIC_MODEL) are required for a live run.", file=sys.stderr)
        return 2

    selected = [s for s in scenarios if not args.ids or s.id in args.ids][: args.limit]
    out_dir = args.out or RESULTS_DIR / f"run-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    with tempfile.TemporaryDirectory() as tmp:
        results = asyncio.run(run_all(selected, AnthropicHTTP(api_key, args.model), Path(tmp), date.today()))
    summary = summarize(results, matrix)
    write_run(out_dir, results, summary)
    print(format_summary(summary))
    print(f"artifacts: {out_dir}")
    return 1 if summary["status"] == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
