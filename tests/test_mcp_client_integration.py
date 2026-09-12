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
from datetime import date, timedelta
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


async def test_list_tools_exposes_all_expected_tools_with_schemas_and_annotations(client):
    tools = {tool.name: tool for tool in await client.list_tools()}
    assert set(tools) == {
        # Layer 1: data
        "read_health_data",
        "log_daily_metric",
        "clear_metric",
        "export_health_data_csv",
        "get_metric_history",
        # Layer 2: analytics
        "get_baseline",
        "detect_metric_anomalies",
        "calculate_metric_trend",
        "compare_metric_periods",
        "find_metric_correlation",
        # Layer 3: personal intelligence
        "get_recent_changes",
        "explain_metric_change",
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


async def test_analytics_tools_round_trip_through_the_protocol(client):
    """Log a small, deliberately-anomalous series and exercise each new
    Layer-2 tool through the actual protocol layer, the same way the rest
    of this file tests read_health_data/log_daily_metric/clear_metric.
    """
    sleep_values = [7.0, 7.2, 6.8, 7.1, 6.9, 7.0, 2.0]  # last day is a clear low outlier
    for i, value in enumerate(sleep_values, start=1):
        await client.call_tool("log_daily_metric", {"date": f"2026-04-{i:02d}", "sleep_hours": value})

    baseline = await client.call_tool(
        "get_baseline", {"metric": "sleep_hours", "start_date": "2026-04-01", "end_date": "2026-04-07"}
    )
    assert baseline.structured_content["baseline"]["n"] == 7

    anomalies = await client.call_tool(
        "detect_metric_anomalies", {"metric": "sleep_hours", "start_date": "2026-04-01", "end_date": "2026-04-07"}
    )
    flagged = anomalies.structured_content["anomalies"]
    assert len(flagged) == 1
    assert flagged[0]["date"] == "2026-04-07"
    assert flagged[0]["direction"] == "below"

    trend = await client.call_tool(
        "calculate_metric_trend", {"metric": "sleep_hours", "start_date": "2026-04-01", "end_date": "2026-04-07"}
    )
    assert trend.structured_content["trend"]["direction"] in {"decreasing", "flat", "increasing"}

    history = await client.call_tool(
        "get_metric_history", {"metric": "sleep_hours", "start_date": "2026-04-01", "end_date": "2026-04-07"}
    )
    assert len(history.structured_content["points"]) == 7


async def test_compare_and_correlate_tools(client):
    for i in range(1, 8):
        await client.call_tool("log_daily_metric", {"date": f"2026-05-{i:02d}", "steps": 5000, "mood": 4})
    for i in range(8, 15):
        await client.call_tool("log_daily_metric", {"date": f"2026-05-{i:02d}", "steps": 9000, "mood": 8})

    comparison = await client.call_tool(
        "compare_metric_periods",
        {
            "metric": "steps",
            "period_a_start": "2026-05-08",
            "period_a_end": "2026-05-14",
            "period_b_start": "2026-05-01",
            "period_b_end": "2026-05-07",
        },
    )
    assert comparison.structured_content["delta"] == 4000
    assert comparison.structured_content["pct_change"] == 80.0

    correlation = await client.call_tool(
        "find_metric_correlation",
        {"metric_a": "steps", "metric_b": "mood", "start_date": "2026-05-01", "end_date": "2026-05-14"},
    )
    # Both series jump together at the same day, so this should be a strong positive correlation.
    assert correlation.structured_content["r"] > 0.9


async def test_analytics_tools_reject_a_private_metric(client, monkeypatch):
    """A metric configured as private must be refused by the analytics
    tools outright, the same way it's redacted by read_health_data — a
    baseline or anomaly computed from it would leak its shape even
    without ever printing a raw value.
    """
    monkeypatch.setenv("HEALTH_PRIVATE_FIELDS", "mood")
    sys.modules.pop("server", None)
    import server as private_server

    async with Client(private_server.mcp) as private_client:
        with pytest.raises(ToolError, match="invalid_field"):
            await private_client.call_tool("get_baseline", {"metric": "mood"})


async def test_get_recent_changes_and_explain_metric_change(client):
    # 15 days with a little natural day-to-day variance (a perfectly flat
    # baseline makes the MAD-based detector's spread collapse to zero, by
    # design — see analytics.detect_anomalies), then a single sharp drop
    # on the most recent day.
    baseline_steps = [7800, 7900, 8000, 8100, 8200, 7850, 7950, 8050, 8150, 7825, 7975, 8025, 8125, 7875, 8075]
    today = date.today()
    for offset, steps in zip(range(15, 0, -1), baseline_steps, strict=True):
        day = today - timedelta(days=offset)
        await client.call_tool("log_daily_metric", {"date": day.isoformat(), "steps": steps})
    await client.call_tool("log_daily_metric", {"date": today.isoformat(), "steps": 2000})

    recent = await client.call_tool("get_recent_changes", {"days": 3})
    metrics_flagged = {c["metric"] for c in recent.structured_content["changes"]}
    assert "steps" in metrics_flagged

    explanation = await client.call_tool("explain_metric_change", {"metric": "steps", "date": today.isoformat()})
    result = explanation.structured_content
    assert result["value"] == 2000
    assert result["is_anomaly"] is True
    assert any("anomaly" in fact for fact in result["narrative_facts"])
