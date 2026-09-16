"""
Routing tests: does a real Claude model, given this server's actual tool
schemas (fetched live via fastmcp.Client, the same fixture pattern as
test_mcp_client_integration.py), pick the *correct* tool for a natural-
language prompt?

This is deliberately a different question from test_mcp_client_integration.py
(which proves the protocol plumbing works once a tool is already chosen) and
from test_server.py (which proves each tool's own logic is correct). Neither
of those catches a model reaching for read_health_data when the user meant
read_measurements, or for get_baseline when they meant calculate_metric_trend
— that's a description/routing problem, not a logic bug, and it only shows
up by actually asking a model to choose.

Requires a live Anthropic API key and costs real tokens (one call per case,
tool_choice forced to "any" so the model must pick a tool rather than reply
in prose). Skipped entirely — not failed — when ANTHROPIC_API_KEY or
ROUTING_TEST_MODEL is unset, so `pytest -q` stays green in CI without
credentials. Run locally with both set to actually exercise it:

    ANTHROPIC_API_KEY=sk-... ROUTING_TEST_MODEL=claude-... pytest -q tests/test_tool_routing.py

ROUTING_TEST_MODEL has no hardcoded default on purpose: model ids change
over time and a stale default would silently test against the wrong model
rather than failing loudly.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from fastmcp import Client

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

API_KEY = os.environ.get("ANTHROPIC_API_KEY")
MODEL = os.environ.get("ROUTING_TEST_MODEL")
SKIP_REASON = "ANTHROPIC_API_KEY and ROUTING_TEST_MODEL must both be set to run live routing tests"

# ---------------------------------------------------------------------------
# Cases: (prompt, expected_tool, tier)
#
# "clean"      — unambiguous, sanity-check cases; should always pass.
# "boundary"   — the actual point of this suite. Each targets a specific
#                pair/cluster of semantically adjacent tools called out in
#                the P0 report: read_health_data vs read_measurements vs
#                read_workout_sessions vs get_metric_history, and the five
#                Layer-2 analytics tools vs each other and vs
#                explain_metric_change/get_recent_changes.
# "write"      — logging tools should never be picked for a read question
#                and vice versa.
# ---------------------------------------------------------------------------
ROUTING_CASES = [
    # --- clean ---
    ("What was my resting heart rate last week?", "get_metric_history", "clean"),
    ("Show me my individual HRV measurements with timestamps.", "read_measurements", "clean"),
    ("What workouts did I do last week?", "read_workout_sessions", "clean"),
    ("Log today's weight as 91.4 kg.", "log_daily_metric", "clean"),
    ("Record a blood pressure reading of 120 from my cuff at 7am today.", "log_measurement", "clean"),
    ("I went running for 40 minutes this morning.", "log_workout_session", "clean"),
    ("Give me a broad overview of my health data this month.", "read_health_data", "clean"),
    ("Delete/clear the mood value I logged for yesterday, it was wrong.", "clear_metric", "clean"),
    ("Export my last 90 days of health data to a CSV file.", "export_health_data_csv", "clean"),
    ("Does my sleep affect my mood?", "find_metric_correlation", "clean"),
    ("What's changed in my health data over the last week?", "get_recent_changes", "clean"),
    ("Why was my HRV so low on March 3rd?", "explain_metric_change", "clean"),

    # --- boundary: read_health_data vs read_measurements vs get_metric_history ---
    ("Show me my HRV.", "get_metric_history", "boundary"),
    ("What health data do I have recorded?", "read_health_data", "boundary"),
    (
        "Where did my resting heart rate readings on Tuesday come from — watch or manual entry?",
        "get_metric_provenance",
        "boundary",
    ),

    # --- boundary: read_workout_sessions vs read_health_data (workout_minutes) ---
    ("How many total workout minutes did I log this week?", "read_health_data", "boundary"),
    ("What kind of workouts have I been doing — running, cycling, strength?", "read_workout_sessions", "boundary"),

    # --- boundary: the five Layer-2 analytics tools vs each other ---
    ("What's normal for my resting heart rate?", "get_baseline", "boundary"),
    ("Is my weight trending down?", "calculate_metric_trend", "boundary"),
    ("Was there anything unusual about my sleep last month?", "detect_metric_anomalies", "boundary"),
    ("Compare my average steps this month to last month.", "compare_metric_periods", "boundary"),
    ("Did my HRV change after I increased my workouts?", "find_metric_correlation", "boundary"),

    # --- boundary: analytics vs the composite intelligence tools ---
    ("Give me everything relevant to why my sleep tanked on the 5th.", "explain_metric_change", "boundary"),
    ("How have I been doing overall lately?", "get_recent_changes", "boundary"),

    # --- write vs read, same metric ---
    ("What's my current logged weight?", "get_metric_history", "write"),
    ("Set today's water intake to 2000 ml.", "log_daily_metric", "write"),
]


@pytest.fixture
def health_db(tmp_path, monkeypatch):
    db_path = tmp_path / "health.db"
    monkeypatch.setenv("HEALTH_DB_PATH", str(db_path))
    sys.modules.pop("server", None)
    import server

    return server


@pytest.fixture
async def anthropic_tool_schemas(health_db):
    """Live tool schemas from this server, converted to Anthropic API shape.
    Fetched the same way test_mcp_client_integration.py does (in-memory
    fastmcp.Client), so this suite tests against the schemas the server
    actually serves, not a hand-copied snapshot that could drift.
    """
    async with Client(health_db.mcp) as client:
        tools = await client.list_tools()
    return [
        {
            "name": t.name,
            "description": t.description or "",
            "input_schema": t.inputSchema,
        }
        for t in tools
    ]


def _call_anthropic(prompt: str, tools: list[dict]) -> str:
    """POST to /v1/messages with tool_choice forced to 'any' so the model
    must select a tool rather than answer in prose. Returns the selected
    tool's name. Raises AssertionError with the raw response on any
    transport/API error so a failure is diagnosable, not a bare traceback.
    """
    body = json.dumps(
        {
            "model": MODEL,
            "max_tokens": 512,
            "tools": tools,
            "tool_choice": {"type": "any"},
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": API_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise AssertionError(f"Anthropic API error {exc.code}: {exc.read().decode('utf-8')}") from exc

    tool_uses = [block for block in data.get("content", []) if block.get("type") == "tool_use"]
    assert tool_uses, f"Model returned no tool_use block for prompt {prompt!r}: {data}"
    return tool_uses[0]["name"]


@pytest.mark.skipif(not (API_KEY and MODEL), reason=SKIP_REASON)
@pytest.mark.parametrize("prompt,expected_tool,tier", ROUTING_CASES, ids=[c[0] for c in ROUTING_CASES])
async def test_prompt_routes_to_expected_tool(anthropic_tool_schemas, prompt, expected_tool, tier):
    selected = _call_anthropic(prompt, anthropic_tool_schemas)
    assert selected == expected_tool, (
        f"[{tier}] {prompt!r} -> expected {expected_tool!r}, model picked {selected!r}"
    )
