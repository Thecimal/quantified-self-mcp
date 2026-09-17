#!/usr/bin/env python3
"""Tool-routing evaluation harness for quantified-self-mcp.

Answers one question: given the server's *real*, live tool list (names +
descriptions, introspected over MCP -- not hand-copied), does an LLM pick
the tool a human reviewer would consider correct for a batch of realistic
prompts, including prompts deliberately written to sit on the boundary
between two tools?

This only tests routing (which tool gets called), not execution -- it
does not require a populated database, and it does not chain multiple
tool calls. That keeps it fast and deterministic to re-run after every
docstring or tool-surface change.

Usage:
    export ANTHROPIC_API_KEY=...
    python run_eval.py --dump-tools --server-cmd "quantified-self-mcp"

    python run_eval.py \
        --server-cmd "quantified-self-mcp" \
        --prompts prompts.yaml \
        --out results.json

`--server-cmd` is whatever command starts the MCP server over stdio --
for this project that's the `quantified-self-mcp` console-script entry
point installed by `pip install -e .`, not a `python -m` module path.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import yaml

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError:
    print(
        "Missing dependency 'mcp'. Install with:\n"
        "  pip install -r eval/tool_routing/requirements-eval.txt",
        file=sys.stderr,
    )
    raise

try:
    import anthropic
except ImportError:
    print(
        "Missing dependency 'anthropic'. Install with:\n"
        "  pip install -r eval/tool_routing/requirements-eval.txt",
        file=sys.stderr,
    )
    raise


DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

SYSTEM_PROMPT = (
    "You are the assistant embedded in a personal quantified-self app. "
    "The user is asking about their own health data, which is only "
    "accessible through the tools provided. For every question, call the "
    "single tool that best answers it. Do not ask a clarifying question "
    "first -- pick the most reasonable interpretation and call a tool. "
    "Do not call more than one tool unless the question genuinely has two "
    "separate parts."
)


@dataclass
class PromptCase:
    id: str
    category: str
    prompt: str
    canonical: str
    acceptable: list[str] = field(default_factory=list)


@dataclass
class Verdict:
    case: PromptCase
    called_tools: list[str]
    verdict: str  # canonical | acceptable | wrong | no_tool | multi_tool_wrong
    raw_text: str = ""


async def fetch_live_tools(server_cmd: str) -> list[Any]:
    """Connect to the MCP server over stdio and return its tool list."""
    parts = shlex.split(server_cmd)
    params = StdioServerParameters(command=parts[0], args=parts[1:])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            resp = await session.list_tools()
            return list(resp.tools)


def to_anthropic_tools(mcp_tools: list[Any]) -> list[dict]:
    tools = []
    for t in mcp_tools:
        schema = getattr(t, "inputSchema", None) or {
            "type": "object",
            "properties": {},
        }
        tools.append(
            {
                "name": t.name,
                "description": t.description or "",
                "input_schema": schema,
            }
        )
    return tools


def load_prompts(path: str) -> list[PromptCase]:
    with open(path, "r") as f:
        raw = yaml.safe_load(f)
    cases = []
    for entry in raw:
        cases.append(
            PromptCase(
                id=str(entry["id"]),
                category=entry["category"],
                prompt=entry["prompt"],
                canonical=entry["canonical"],
                acceptable=entry.get("acceptable") or [],
            )
        )
    return cases


def validate_tool_names(cases: list[PromptCase], live_names: set[str]) -> None:
    referenced = set()
    for c in cases:
        referenced.add(c.canonical)
        referenced.update(c.acceptable)
    unknown = sorted(referenced - live_names)
    if unknown:
        print(
            "WARNING: prompts.yaml references tool name(s) not found on "
            f"the live server: {unknown}\n"
            "  Run with --dump-tools to see the actual tool names/"
            "descriptions and fix prompts.yaml accordingly. Cases "
            "referencing these names will likely score as 'wrong' even "
            "when the model's real choice was reasonable.\n",
            file=sys.stderr,
        )


def score(case: PromptCase, called_tools: list[str]) -> str:
    if not called_tools:
        return "no_tool"
    if len(called_tools) == 1:
        name = called_tools[0]
        if name == case.canonical:
            return "canonical"
        if name in case.acceptable:
            return "acceptable"
        return "wrong"
    # more than one tool called in a single turn
    allowed = {case.canonical, *case.acceptable}
    if all(n in allowed for n in called_tools):
        return "multi_tool_acceptable"
    return "multi_tool_wrong"


def run_case(
    client: "anthropic.Anthropic",
    model: str,
    tools: list[dict],
    case: PromptCase,
) -> Verdict:
    resp = client.messages.create(
        model=model,
        max_tokens=512,
        system=SYSTEM_PROMPT,
        tools=tools,
        tool_choice={"type": "auto"},
        messages=[{"role": "user", "content": case.prompt}],
    )
    called = [b.name for b in resp.content if b.type == "tool_use"]
    text = "".join(b.text for b in resp.content if b.type == "text")
    verdict = score(case, called)
    return Verdict(case=case, called_tools=called, verdict=verdict, raw_text=text)


def print_report(verdicts: list[Verdict]) -> None:
    by_category: dict[str, list[Verdict]] = defaultdict(list)
    for v in verdicts:
        by_category[v.case.category].append(v)

    good = {"canonical", "acceptable", "multi_tool_acceptable"}

    print("\n=== Tool-routing eval report ===\n")
    header = f"{'category':<24}{'n':>4}{'pass':>6}{'pass%':>8}"
    print(header)
    print("-" * len(header))
    total_n, total_pass = 0, 0
    for cat, vs in sorted(by_category.items()):
        n = len(vs)
        p = sum(1 for v in vs if v.verdict in good)
        total_n += n
        total_pass += p
        print(f"{cat:<24}{n:>4}{p:>6}{p / n * 100:>7.0f}%")
    print("-" * len(header))
    print(
        f"{'TOTAL':<24}{total_n:>4}{total_pass:>6}"
        f"{(total_pass / total_n * 100 if total_n else 0):>7.0f}%"
    )

    print("\n--- Failures (wrong / no_tool / multi_tool_wrong) ---")
    failures = [v for v in verdicts if v.verdict not in good]
    if not failures:
        print("None.")
    for v in failures:
        print(
            f"\n[{v.case.id}] ({v.case.category}) {v.verdict}\n"
            f"  prompt:    {v.case.prompt}\n"
            f"  expected:  {v.case.canonical} "
            f"(acceptable: {v.case.acceptable or '-'})\n"
            f"  got:       {v.called_tools or '(none, model answered in text)'}"
        )


def write_json(verdicts: list[Verdict], out_path: str) -> None:
    payload = [
        {
            "id": v.case.id,
            "category": v.case.category,
            "prompt": v.case.prompt,
            "canonical": v.case.canonical,
            "acceptable": v.case.acceptable,
            "called_tools": v.called_tools,
            "verdict": v.verdict,
        }
        for v in verdicts
    ]
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"\nWrote {len(payload)} results to {out_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--server-cmd",
        required=True,
        help="Command to launch the MCP server over stdio, "
        'e.g. "quantified-self-mcp" (the console-script entry point)',
    )
    ap.add_argument(
        "--prompts",
        default=os.path.join(os.path.dirname(__file__), "prompts.yaml"),
        help="Path to the prompts YAML file (default: prompts.yaml next to this script)",
    )
    ap.add_argument("--out", default="results.json", help="Where to write JSON results")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="Anthropic model id to test")
    ap.add_argument(
        "--limit", type=int, default=None, help="Only run the first N prompts"
    )
    ap.add_argument(
        "--category", default=None, help="Only run prompts in this category"
    )
    ap.add_argument(
        "--dump-tools",
        action="store_true",
        help="List the live server's tools (name + description) and exit. "
        "Use this to fix tool names in prompts.yaml.",
    )
    args = ap.parse_args()

    live_tools = asyncio.run(fetch_live_tools(args.server_cmd))

    if args.dump_tools:
        print(f"{len(live_tools)} tools on live server:\n")
        for t in sorted(live_tools, key=lambda t: t.name):
            desc = (t.description or "").strip().splitlines()[0] if t.description else ""
            print(f"- {t.name}\n    {desc}\n")
        return

    if "ANTHROPIC_API_KEY" not in os.environ:
        print("ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        sys.exit(1)

    anthropic_tools = to_anthropic_tools(live_tools)
    live_names = {t["name"] for t in anthropic_tools}

    cases = load_prompts(args.prompts)
    if args.category:
        cases = [c for c in cases if c.category == args.category]
    if args.limit:
        cases = cases[: args.limit]

    validate_tool_names(cases, live_names)

    client = anthropic.Anthropic()
    verdicts = []
    for case in cases:
        v = run_case(client, args.model, anthropic_tools, case)
        verdicts.append(v)
        print(f"[{case.id}] {case.category:<24} {v.verdict:<20} {case.prompt}")

    print_report(verdicts)
    write_json(verdicts, args.out)


if __name__ == "__main__":
    main()
