"""Coverage matrix: which registered tools have which kinds of scenario.

A tool counts only for scenarios that declare it as their `tool`; being called
incidentally inside another scenario earns no coverage. The tool list comes
from the live server, so a newly registered tool shows up as a gap by itself.
"""

from __future__ import annotations

from typing import Any

from eval.agent.permissions import BOUNDARY, Boundary
from eval.agent.scenarios import KINDS, Scenario


def coverage_matrix(
    registered: set[str], scenarios: list[Scenario], boundary: dict[str, Boundary] = BOUNDARY
) -> dict[str, Any]:
    rows: dict[str, dict[str, int]] = {tool: dict.fromkeys(KINDS, 0) for tool in sorted(registered)}
    for scenario in scenarios:
        if scenario.tool in rows:
            rows[scenario.tool][scenario.kind] += 1
    represented = [tool for tool, counts in rows.items() if sum(counts.values()) > 0]
    gaps = {"represented": sorted(registered - set(represented))}
    for kind in KINDS:
        gaps[kind] = [tool for tool, counts in rows.items() if counts[kind] == 0]
    # Permission coverage is reported on its own: a tool is permission-sensitive (it needs a permission scenario),
    # NOT_APPLICABLE (the evaluator's BOUNDARY says why) or unclassified (a new tool nobody has classified yet).
    sensitive = sorted(t for t in registered if t in boundary and boundary[t].applicable)
    permission = {
        "sensitive": len(sensitive),
        "covered": sum(1 for t in sensitive if rows[t]["permission"] > 0),
        "not_applicable": {
            t: boundary[t].reason for t in sorted(registered) if t in boundary and not boundary[t].applicable
        },
        "unclassified": sorted(registered - set(boundary)),
        "gaps": [t for t in sensitive if rows[t]["permission"] == 0],
    }
    return {
        "permission": permission,
        "tools": len(rows),
        "represented": len(represented),
        "by_kind": {kind: len(rows) - len(gaps[kind]) for kind in KINDS},
        "gaps": gaps,
        "rows": rows,
    }


def format_matrix(matrix: dict[str, Any]) -> str:
    total = matrix["tools"]
    lines = [f"{matrix['represented']}/{total} tools represented"]
    lines += [
        f"{matrix['by_kind'][kind]}/{total} tools have a {kind} scenario" for kind in KINDS if kind != "permission"
    ]
    perm = matrix["permission"]
    lines.append(f"{perm['covered']}/{perm['sensitive']} permission-sensitive tools have a permission scenario")
    lines.append(
        f"{len(perm['not_applicable'])} tools NOT_APPLICABLE to permission: "
        + (", ".join(perm["not_applicable"]) or "none")
    )
    if perm["gaps"]:
        lines.append("permission NOT covered: " + ", ".join(perm["gaps"]))
    if perm["unclassified"]:
        lines.append("permission UNCLASSIFIED: " + ", ".join(perm["unclassified"]))
    if matrix["gaps"]["represented"]:
        lines.append("NOT represented: " + ", ".join(matrix["gaps"]["represented"]))
    return "\n".join(lines)
