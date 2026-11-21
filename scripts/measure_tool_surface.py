#!/usr/bin/env python3
"""Measure the serialized MCP tool-listing surface for this server.

This reproduces, offline and deterministically, the kind of measurement an
MCP client pays for once at session start: the bytes of `tools/list` (every
registered tool's name, description, input schema and output schema) plus
the server-level `instructions` string sent at `initialize`. That payload is
what actually lands in a model's context, not this repo's docstrings as
written in source (FastMCP drops a docstring's "Returns:" section from the
protocol-level description entirely -- see tests/test_server.py -- so it
costs nothing on the wire regardless of how it's written).

Token counts here are a deterministic, dependency-free approximation
(chars / 4, the common rule-of-thumb ratio for English text and JSON), not a
real BPE tokenizer -- intentionally, so this script and the regression test
that shares its measurement function never depend on network access or a
pinned tokenizer vocabulary. Treat the numbers as directionally accurate and
reproducible run-to-run, not as an exact count for any specific model's
tokenizer. An independent measurement using a real tokenizer produced
~14,242 tokens for this server before this change; this script's
approximation is calibrated against that to within a few percent (see
README/CHANGELOG for the before/after this script produced).

Usage:
    python scripts/measure_tool_surface.py              # summary + per-tool table
    python scripts/measure_tool_surface.py --json        # machine-readable
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from fastmcp import Client  # noqa: E402

from eval.agent.runner import loaded_server  # noqa: E402


def approx_tokens(text: str) -> float:
    """chars/4: a deterministic, offline, tokenizer-free approximation."""
    return len(text) / 4


async def _collect(tmp_path: Path) -> dict:
    with loaded_server(tmp_path / "health.db", "") as server:
        async with Client(server.mcp) as client:
            tools = await client.list_tools()
            instructions = server.mcp.instructions or ""
            per_tool = []
            for tool in sorted(tools, key=lambda t: t.name):
                payload = {
                    "name": tool.name,
                    "description": tool.description or "",
                    "inputSchema": tool.input_schema,
                    "outputSchema": tool.output_schema,
                }
                serialized = json.dumps(payload, separators=(",", ":"))
                per_tool.append(
                    {
                        "name": tool.name,
                        "description_chars": len(tool.description or ""),
                        "serialized_bytes": len(serialized),
                        "approx_tokens": approx_tokens(serialized),
                    }
                )
            instructions_serialized = json.dumps(instructions)
            total_bytes = sum(t["serialized_bytes"] for t in per_tool) + len(instructions_serialized)
            total_tokens = sum(t["approx_tokens"] for t in per_tool) + approx_tokens(instructions_serialized)
            return {
                "tool_count": len(tools),
                "instructions_bytes": len(instructions_serialized),
                "instructions_approx_tokens": approx_tokens(instructions_serialized),
                "tools": per_tool,
                "total_bytes": total_bytes,
                "total_approx_tokens": total_tokens,
            }


def measure() -> dict:
    with tempfile.TemporaryDirectory() as d:
        return asyncio.run(_collect(Path(d)))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON instead of a table")
    args = parser.parse_args(argv)

    result = measure()
    if args.json:
        print(json.dumps(result, indent=2))
        return

    print(f"{'tool':<34}{'desc chars':>12}{'bytes':>10}{'~tokens':>10}")
    print("-" * 66)
    for row in result["tools"]:
        print(
            f"{row['name']:<34}{row['description_chars']:>12}{row['serialized_bytes']:>10}{row['approx_tokens']:>10.0f}"
        )
    print("-" * 66)
    instr_bytes = result["instructions_bytes"]
    instr_tokens = result["instructions_approx_tokens"]
    print(f"{'instructions (server-level)':<34}{'':>12}{instr_bytes:>10}{instr_tokens:>10.0f}")
    print("-" * 66)
    print(f"tools registered: {result['tool_count']}")
    print(f"total serialized bytes:  {result['total_bytes']}")
    print(f"total approx tokens:     {result['total_approx_tokens']:.0f}")


if __name__ == "__main__":
    main()
