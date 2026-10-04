"""Deterministic scoring of one trace, layer by layer.

Phase 1 scores routing, arguments and execution. Interpretation and final
answer are left as None ("not scored") until their validators exist; the
summary reports them as NOT_SCORED rather than counting them as passed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from eval.agent.loop import Trace
from eval.agent.scenarios import WRITE_TOOLS, Scenario, resolve

LAYERS = ("routing", "arguments", "execution", "interpretation", "grounding")
CRITICAL_CODES = frozenset({"tool_hallucination", "forbidden_tool_called"})


@dataclass
class Score:
    routing: bool
    arguments: bool | None  # None: the scenario pins no arguments
    execution: bool
    interpretation: bool | None = None
    grounding: bool | None = None
    failures: list[dict[str, str]] = field(default_factory=list)
    critical: list[str] = field(default_factory=list)

    def layers(self) -> dict[str, bool | None]:
        return {name: getattr(self, name) for name in LAYERS}

    @property
    def passed(self) -> bool:
        return not self.critical and all(v is not False for v in self.layers().values())


def _values_match(actual: Any, expected: Any) -> bool:
    numeric = (int, float)
    if isinstance(actual, numeric) and isinstance(expected, numeric) and not isinstance(actual, bool):
        return abs(actual - expected) < 1e-9
    return actual == expected


def score_trace(scenario: Scenario, trace: Trace, readonly: dict[str, bool], today: date) -> Score:
    failures: list[dict[str, str]] = []
    critical: list[str] = []

    def fail(layer: str, code: str, detail: str) -> None:
        failures.append({"layer": layer, "code": code, "detail": detail})
        if code in CRITICAL_CODES:
            critical.append(code)

    called = trace.called_tools()
    write_tools = {name for name, is_readonly in readonly.items() if not is_readonly}
    forbidden_names = scenario.forbidden_tools
    if forbidden_names is None:
        forbidden_names = [WRITE_TOOLS] if readonly.get(scenario.tool, True) else []
    forbidden = set()
    for name in forbidden_names:
        forbidden |= write_tools if name == WRITE_TOOLS else {name}

    # routing
    routing = True
    if not called:
        routing = False
        fail("routing", "no_tool_call", "the model answered without calling any tool")
    for tool in scenario.must_call:
        if called and tool not in called:
            routing = False
            fail("routing", "missing_required_tool", f"{tool} was never called")
    for call in trace.tool_calls:
        if not call.registered:
            routing = False
            fail("routing", "tool_hallucination", f"{call.tool} is not a registered tool")
        elif call.tool in forbidden:
            routing = False
            fail("routing", "forbidden_tool_called", f"{call.tool} must not be called for this question")
    if scenario.allowed_tools is not None:
        permitted = {*scenario.allowed_tools, *scenario.must_call}
        for call in trace.tool_calls:
            if call.registered and call.tool not in permitted and call.tool not in forbidden:
                routing = False
                fail("routing", "unexpected_tool", f"{call.tool} is outside the allowed set for this question")

    # arguments
    arguments: bool | None = None
    if scenario.arguments:
        expected = resolve(scenario.arguments, today)
        target = scenario.must_call[0]
        attempts = [c.arguments for c in trace.tool_calls if c.tool == target]
        arguments = any(all(_values_match(a.get(k), v) for k, v in expected.items()) for a in attempts)
        if not arguments:
            fail("arguments", "argument_mismatch", f"expected {expected} on {target}, got {attempts or 'no call'}")

    # execution
    execution = True
    for call in trace.tool_calls:
        if call.registered and call.is_error and not scenario.expect_error:
            execution = False
            fail("execution", "tool_error", f"{call.tool} returned an error: {call.result}")
        if not call.registered:
            execution = False
            fail("execution", "unregistered_tool", f"{call.tool} could not be executed")
    if trace.terminated != "end_turn":
        execution = False
        fail("execution", "agent_did_not_finish", f"the loop stopped with {trace.terminated!r}")

    return Score(
        routing=routing,
        arguments=arguments,
        execution=execution,
        failures=failures,
        critical=sorted(set(critical)),
    )
