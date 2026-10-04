"""The agent loop: question -> model -> real MCP tool call -> real result -> model -> ... -> final answer.

Tools are executed on the real, registered server through fastmcp.Client, so
the schemas the model sees and the implementations that answer are exactly
what a client gets. Nothing here scores anything; it only records a Trace.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from eval.agent.model import ModelClient

MAX_TURNS = 8


@dataclass
class ToolCall:
    tool: str
    arguments: dict[str, Any]
    is_error: bool
    registered: bool
    result: Any


@dataclass
class Trace:
    scenario: str
    model: str
    prompt: str
    system: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    final_answer: str = ""
    terminated: str = "max_turns"  # "end_turn" once the model answers without calling a tool
    turns: int = 0

    def called_tools(self) -> list[str]:
        return [call.tool for call in self.tool_calls]

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "model": self.model,
            "prompt": self.prompt,
            "system": self.system,
            "tool_trace": [
                {
                    "tool": c.tool,
                    "arguments": c.arguments,
                    "is_error": c.is_error,
                    "registered": c.registered,
                    "result": c.result,
                }
                for c in self.tool_calls
            ],
            "final_answer": self.final_answer,
            "terminated": self.terminated,
            "turns": self.turns,
        }


def anthropic_tools(mcp_tools: list[Any]) -> list[dict[str, Any]]:
    return [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema} for t in mcp_tools]


def _clean_block(block: dict[str, Any]) -> dict[str, Any]:
    if block.get("type") == "tool_use":
        return {"type": "tool_use", "id": block["id"], "name": block["name"], "input": block.get("input") or {}}
    if block.get("type") == "text":
        return {"type": "text", "text": block.get("text", "")}
    return block


async def _execute(client: Any, registered: set[str], use: dict[str, Any]) -> ToolCall:
    name, arguments = use["name"], use.get("input") or {}
    if name not in registered:
        return ToolCall(name, arguments, True, False, {"error": f"unknown tool {name!r}"})
    try:
        result = await client.call_tool(name, arguments, raise_on_error=False)
    except Exception as exc:  # a protocol-level failure is still a tool result the model must see
        return ToolCall(name, arguments, True, True, {"error": f"{type(exc).__name__}: {exc}"})
    if result.structured_content is not None:
        payload: Any = result.structured_content
    else:
        payload = [block.text for block in result.content if hasattr(block, "text")]
    return ToolCall(name, arguments, bool(result.is_error), True, payload)


async def run_agent(
    model: ModelClient,
    client: Any,
    *,
    scenario_id: str,
    question: str,
    system: str,
    max_turns: int = MAX_TURNS,
) -> Trace:
    tools = await client.list_tools()
    registered = {t.name for t in tools}
    schemas = anthropic_tools(tools)
    messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
    trace = Trace(scenario=scenario_id, model=model.name, prompt=question, system=system)

    for turn in range(1, max_turns + 1):
        trace.turns = turn
        response = model.complete(system, messages, schemas)
        blocks = response.get("content", [])
        messages.append({"role": "assistant", "content": [_clean_block(b) for b in blocks]})
        trace.final_answer = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        uses = [b for b in blocks if b.get("type") == "tool_use"]
        if not uses:
            trace.terminated = "end_turn"
            break
        results = []
        for use in uses:
            call = await _execute(client, registered, use)
            trace.tool_calls.append(call)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": use["id"],
                    "content": json.dumps(call.result, default=str),
                    "is_error": call.is_error,
                }
            )
        messages.append({"role": "user", "content": results})
    return trace
