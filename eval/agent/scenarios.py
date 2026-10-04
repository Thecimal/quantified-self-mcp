"""Scenario definitions: what the evaluator (not the model) expects to happen.

Scenarios live in eval/agent/scenarios/*.yaml. Text fields may use the date
placeholders in placeholders(); they are resolved against the run's date, so
expectations stay valid while the fixtures move with the clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

from eval.agent.fixtures import FIXTURES

SCENARIO_DIR = Path(__file__).parent / "scenarios"
KINDS = ("should_use", "should_not_use", "argument", "permission", "interpretation", "grounding")
WRITE_TOOLS = "write_tools"  # alias for every tool whose readOnlyHint is false


@dataclass
class Scenario:
    id: str
    tool: str  # the tool this scenario is about, for the coverage matrix
    kind: str
    fixture: str
    question: str
    must_call: list[str]
    allowed_tools: list[str] | None = None  # when set, calling anything outside it + must_call is a routing failure
    forbidden_tools: list[str] | None = None  # None: write tools are forbidden whenever `tool` is read-only
    arguments: dict[str, Any] = field(default_factory=dict)  # expected arguments of the first must_call tool
    expect_error: bool = False


def placeholders(today: date) -> dict[str, str]:
    return {
        "today": today.isoformat(),
        "yesterday": (today - timedelta(days=1)).isoformat(),
        "d3": (today - timedelta(days=3)).isoformat(),
        "d7": (today - timedelta(days=7)).isoformat(),
        "d14": (today - timedelta(days=14)).isoformat(),
        "d30": (today - timedelta(days=30)).isoformat(),
    }


def resolve(value: Any, today: date) -> Any:
    if isinstance(value, str):
        return value.format_map(placeholders(today))
    if isinstance(value, dict):
        return {k: resolve(v, today) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, today) for v in value]
    return value


def load_scenarios(directory: Path = SCENARIO_DIR) -> list[Scenario]:
    scenarios: list[Scenario] = []
    for path in sorted(directory.glob("*.yaml")):
        for entry in yaml.safe_load(path.read_text(encoding="utf-8")) or []:
            scenarios.append(Scenario(**entry))
    problems = validate_scenarios(scenarios)
    if problems:
        raise ValueError("invalid scenarios:\n  " + "\n  ".join(problems))
    return scenarios


def validate_scenarios(scenarios: list[Scenario], registered: set[str] | None = None) -> list[str]:
    """Structural problems, plus unknown tool names when the live tool list is supplied."""
    problems: list[str] = []
    seen: set[str] = set()
    for s in scenarios:
        if s.id in seen:
            problems.append(f"{s.id}: duplicate id")
        seen.add(s.id)
        if s.kind not in KINDS:
            problems.append(f"{s.id}: unknown kind {s.kind!r}")
        if s.fixture not in FIXTURES:
            problems.append(f"{s.id}: unknown fixture {s.fixture!r}")
        if s.kind == "should_use" and s.tool not in s.must_call:
            problems.append(f"{s.id}: a should_use scenario must list its own tool {s.tool!r} in must_call")
        if registered is not None:
            named = {s.tool, *s.must_call, *(s.allowed_tools or []), *(s.forbidden_tools or [])} - {WRITE_TOOLS}
            for name in sorted(named - registered):
                problems.append(f"{s.id}: tool {name!r} is not registered on the server")
    return problems
