"""Summaries, gates and replayable artifacts for a run."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from eval.agent.scoring import LAYERS

THRESHOLDS = {"routing": 0.95, "arguments": 0.95, "execution": 0.98, "interpretation": 0.95, "grounding": 0.95}


def summarize(results: list[Any], coverage: dict[str, Any] | None = None) -> dict[str, Any]:
    gates: dict[str, dict[str, Any]] = {}
    for layer in LAYERS:
        scored = [getattr(r.score, layer) for r in results if getattr(r.score, layer) is not None]
        rate = sum(1 for v in scored if v) / len(scored) if scored else None
        if rate is None:
            status = "NOT_SCORED"
        else:
            status = "PASS" if rate >= THRESHOLDS[layer] else "FAIL"
        gates[layer] = {"threshold": THRESHOLDS[layer], "scored": len(scored), "rate": rate, "status": status}
    critical = [{"scenario": r.scenario.id, "code": code} for r in results for code in r.score.critical]
    if critical or any(g["status"] == "FAIL" for g in gates.values()):
        status = "FAIL"
    elif any(g["status"] == "NOT_SCORED" for g in gates.values()):
        status = "INCOMPLETE"
    else:
        status = "PASS"
    return {
        "status": status,
        "scenarios": len(results),
        "passed": sum(1 for r in results if r.score.passed),
        "gates": gates,
        "critical_failures": critical,
        "coverage": coverage,
    }


def write_run(out_dir: Path, results: list[Any], summary: dict[str, Any]) -> None:
    (out_dir / "traces").mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    rows, failures = [], []
    for r in results:
        record = {
            "scenario": r.scenario.id,
            "tool": r.scenario.tool,
            "kind": r.scenario.kind,
            "fixture": r.scenario.fixture,
            "passed": r.score.passed,
            "tool_scores": {k: r.score.layers()[k] for k in ("routing", "arguments", "execution")},
            "answer_scores": {k: r.score.layers()[k] for k in ("interpretation", "grounding")},
            "tools_called": r.trace.called_tools(),
            "failures": r.score.failures,
            "critical": r.score.critical,
        }
        rows.append(json.dumps(record))
        if not r.score.passed:
            failures.append(json.dumps(record))
        extra = {k: record[k] for k in ("tool_scores", "answer_scores", "failures", "critical")}
        trace = {**r.trace.to_dict(), **extra}
        trace_path = out_dir / "traces" / f"{r.scenario.id}.json"
        trace_path.write_text(json.dumps(trace, indent=2, default=str), encoding="utf-8")
    (out_dir / "scenarios.jsonl").write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")
    (out_dir / "failures.jsonl").write_text("\n".join(failures) + ("\n" if failures else ""), encoding="utf-8")


def format_summary(summary: dict[str, Any]) -> str:
    lines = [f"status: {summary['status']}  ({summary['passed']}/{summary['scenarios']} scenarios passed)"]
    for layer, gate in summary["gates"].items():
        rate = "-" if gate["rate"] is None else f"{gate['rate']:.0%}"
        lines.append(f"  {layer:<15} {rate:>5}  (gate {gate['threshold']:.0%}, n={gate['scored']})  {gate['status']}")
    for item in summary["critical_failures"]:
        lines.append(f"  CRITICAL {item['code']} in {item['scenario']}")
    return "\n".join(lines)
