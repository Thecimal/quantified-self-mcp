"""Coverage matrix: which registered tools have which kinds of scenario.

A tool counts only for scenarios that declare it as their `tool`; being called
incidentally inside another scenario earns no coverage. The tool list comes
from the live server, so a newly registered tool shows up as a gap by itself.
"""

from __future__ import annotations

from typing import Any

from eval.agent.scenarios import KINDS, Scenario


def coverage_matrix(registered: set[str], scenarios: list[Scenario]) -> dict[str, Any]:
    rows: dict[str, dict[str, int]] = {tool: dict.fromkeys(KINDS, 0) for tool in sorted(registered)}
    for scenario in scenarios:
        if scenario.tool in rows:
            rows[scenario.tool][scenario.kind] += 1
    represented = [tool for tool, counts in rows.items() if sum(counts.values()) > 0]
    gaps = {"represented": sorted(registered - set(represented))}
    for kind in KINDS:
        gaps[kind] = [tool for tool, counts in rows.items() if counts[kind] == 0]
    return {
        "tools": len(rows),
        "represented": len(represented),
        "by_kind": {kind: len(rows) - len(gaps[kind]) for kind in KINDS},
        "gaps": gaps,
        "rows": rows,
    }


def format_matrix(matrix: dict[str, Any]) -> str:
    total = matrix["tools"]
    lines = [f"{matrix['represented']}/{total} tools represented"]
    lines += [f"{matrix['by_kind'][kind]}/{total} tools have a {kind} scenario" for kind in KINDS]
    if matrix["gaps"]["represented"]:
        lines.append("NOT represented: " + ", ".join(matrix["gaps"]["represented"]))
    return "\n".join(lines)
