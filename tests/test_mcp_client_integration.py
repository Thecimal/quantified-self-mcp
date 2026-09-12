"""
Integration tests that drive server.py through an actual MCP client
(fastmcp.Client, connected in-memory to the FastMCP instance) rather than
calling the tool/resource functions directly as plain Python.

tests/test_server.py calls e.g. health_db.log_daily_metric(...) directly —
fast, and fine for exercising the tools' own logic, but it bypasses the
whole MCP protocol layer: argument schema validation, tool annotations,
output_schema/structured_content population, isError semantics, and
resource listing/reading all live in that layer and are simply never
touched by a direct call. Everything here goes through Client.call_tool /
Client.read_resource instead, so a bug in that layer (like the resource
serialization bug caught while building the resources in server.py —
see issue #18) shows up here even if every direct unit test still passes.

No real subprocess or network transport is used — Client(server.mcp)
connects to the FastMCP instance in-memory, which exercises the same
protocol code path (schema validation, serialization, etc.) without the
cost or flakiness of spawning a real stdio subprocess.
"""

import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def health_db(tmp_path, monkeypatch):
    """A server module instance backed by a fresh, empty database. See
    tests/test_server.py's fixture of the same name for why the module is
    freshly imported per test.
    """
    db_path = tmp_path / "health.db"
    monkeypatch.setenv("HEALTH_DB_PATH", str(db_path))
    sys.modules.pop("server", None)
    import server

    return server


@pytest.fixture
async def client(health_db):
    async with Client(health_db.mcp) as c:
        yield c


async def test_list_tools_exposes_all_four_with_schemas_and_annotations(client):
    tools = {tool.name: tool for tool in await client.list_tools()}
    assert set(tools) == {
        "read_health_data",
        "log_daily_metric",
        "clear_metric",
        "export_health_data_csv",
    }

    # Argument schemas come from the function signature via the protocol
    # layer, not something a direct call would ever check.
    assert "date" in tools["log_daily_metric"].input_schema["properties"]
    assert "steps" in tools["log_daily_metric"].input_schema["properties"]

    # Annotations (#21) are part of what a client sees via list_tools,
    # not just internal metadata.
    assert tools["read_health_data"].annotations.read_only_hint is True
    assert tools["clear_metric"].annotations.destructive_hint is True
    assert tools["export_health_data_csv"].annotations.read_only_hint is True


async def test_call_tool_round_trip_through_the_protocol(client):
    logged = await client.call_tool("log_daily_metric", {"date": "2026-02-01", "steps": 6000, "mood": 7})
    assert logged.structured_content["logged"] == {"steps": 6000, "mood": 7}

    read_back = await client.call_tool(
        "read_health_data", {"start_date": "2026-02-01", "end_date": "2026-02-01"}
    )
    assert read_back.structured_content["rows"][0]["steps"] == 6000
    assert read_back.structured_content["summary"]["days_with_data"] == 1

    cleared = await client.call_tool("clear_metric", {"date": "2026-02-01", "field": "mood"})
    assert cleared.structured_content["row"]["mood"] is None
    assert cleared.structured_content["row"]["steps"] == 6000  # untouched


async def test_call_tool_with_invalid_input_surfaces_as_is_error_with_code(client):
    """A ToolError raised inside a tool becomes isError=True content on a
    successful CallToolResult (MCP tool-execution error semantics), not a
    raised protocol exception — this is exactly the distinction a direct
    function call can't observe, since pytest.raises(ToolError) on a
    direct call is catching a plain Python exception, not this MCP-level
    result shape.
    """
    result = await client.call_tool("log_daily_metric", {"date": "2026-02-02", "mood": 99}, raise_on_error=False)
    assert result.is_error is True
    assert "[invalid_metric_value]" in result.content[0].text


async def test_call_tool_with_missing_required_argument_is_rejected_before_reaching_the_tool_body(client):
    """Argument schema validation happens in the protocol layer before a
    tool's body ever runs — the resulting error message comes from
    pydantic's own schema validation ("Missing required argument"), not
    any of our ERR_* codes from #20. A direct function call can't observe
    this distinction: Python would just raise its own TypeError for a
    missing argument, never exercising the protocol's request validation
    at all.
    """
    with pytest.raises(ToolError, match="field"):
        await client.call_tool("clear_metric", {"date": "2026-02-03"})  # missing required "field"


async def test_resources_are_listed_and_readable_through_the_client(client, health_db):
    health_db.log_daily_metric(date="2026-02-04", steps=1500)

    resources = await client.list_resources()
    assert str(resources[0].uri) == "health://metrics/schema"

    templates = await client.list_resource_templates()
    assert templates[0].uri_template == "health://day/{date}"

    schema = await client.read_resource("health://metrics/schema")
    assert "steps" in schema[0].text

    day = await client.read_resource("health://day/2026-02-04")
    assert '"steps": 1500' in day[0].text


async def test_multi_step_session_stays_consistent_end_to_end(client):
    """A short realistic session — log a few days, read the range back,
    fix a mistake — driven entirely through the client, checking that
    state stays consistent across several protocol round trips rather
    than a single isolated call.
    """
    await client.call_tool("log_daily_metric", {"date": "2026-03-01", "steps": 4000, "sleep_hours": 6.5})
    await client.call_tool("log_daily_metric", {"date": "2026-03-02", "steps": 9000, "sleep_hours": 7.5})
    # Oops, wrong sleep value for the 2nd day — fix it.
    await client.call_tool("clear_metric", {"date": "2026-03-02", "field": "sleep_hours"})
    await client.call_tool("log_daily_metric", {"date": "2026-03-02", "sleep_hours": 8.0})

    read_back = await client.call_tool(
        "read_health_data", {"start_date": "2026-03-01", "end_date": "2026-03-02"}
    )
    rows = {row["date"]: row for row in read_back.structured_content["rows"]}
    assert rows["2026-03-01"]["steps"] == 4000
    assert rows["2026-03-02"]["steps"] == 9000  # untouched by the sleep_hours fix
    assert rows["2026-03-02"]["sleep_hours"] == 8.0
    assert read_back.structured_content["summary"]["days_with_data"] == 2
