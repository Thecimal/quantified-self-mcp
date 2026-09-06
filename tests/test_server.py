"""
Integration tests for the MCP tools in server.py.

Unlike test_logic.py, these need fastmcp installed (it's in
requirements.txt, and requirements-dev.txt pulls that in). Each test gets
its own throwaway database via the health_db fixture, so tests never
touch ./data/health.db or affect each other.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def health_db(tmp_path, monkeypatch):
    """A server module instance backed by a fresh, empty database.

    server.py reads HEALTH_DB_PATH from the environment at import time, so
    the env var is set before a fresh import of the module for each test
    (sys.modules is cleared first in case an earlier test already imported
    it against a different path).
    """
    db_path = tmp_path / "health.db"
    monkeypatch.setenv("HEALTH_DB_PATH", str(db_path))
    sys.modules.pop("server", None)
    import server

    return server


def test_log_then_read_round_trip(health_db):
    logged = json.loads(health_db.log_daily_metric(date="2026-01-01", steps=5000, mood=4))
    assert logged["logged"] == {"steps": 5000, "mood": 4}
    assert logged["row"]["steps"] == 5000
    assert logged["row"]["mood"] == 4

    read_back = json.loads(health_db.read_health_data(start_date="2026-01-01", end_date="2026-01-01"))
    assert read_back["rows"][0]["steps"] == 5000
    assert read_back["rows"][0]["mood"] == 4


def test_log_daily_metric_does_not_clear_other_fields(health_db):
    health_db.log_daily_metric(date="2026-01-02", steps=8000)
    health_db.log_daily_metric(date="2026-01-02", mood=5)
    row = json.loads(health_db.log_daily_metric(date="2026-01-02", water_ml=2000))["row"]
    assert row["steps"] == 8000
    assert row["mood"] == 5
    assert row["water_ml"] == 2000


def test_log_daily_metric_rejects_out_of_range_value(health_db):
    with pytest.raises(ToolError, match="mood"):
        health_db.log_daily_metric(date="2026-01-03", mood=99)


def test_log_daily_metric_requires_at_least_one_metric(health_db):
    with pytest.raises(ToolError):
        health_db.log_daily_metric(date="2026-01-04")


def test_log_daily_metric_rejects_bad_date(health_db):
    with pytest.raises(ToolError):
        health_db.log_daily_metric(date="not-a-date", steps=1000)


def test_clear_metric_blanks_only_the_given_field(health_db):
    health_db.log_daily_metric(date="2026-01-05", steps=9000, mood=3)
    result = json.loads(health_db.clear_metric(date="2026-01-05", field="mood"))
    assert result["row"]["mood"] is None
    assert result["row"]["steps"] == 9000


def test_clear_metric_rejects_unknown_field(health_db):
    with pytest.raises(ToolError):
        health_db.clear_metric(date="2026-01-06", field="not_a_real_field")


def test_clear_metric_on_a_date_with_no_row_reports_nothing_to_clear(health_db):
    result = json.loads(health_db.clear_metric(date="2026-01-07", field="mood"))
    assert "row" not in result
    assert "note" in result


def test_every_tool_carries_the_cloud_model_warning(health_db):
    """Whatever a tool returns is sent to whichever model the MCP client is
    configured with — if that's a cloud model, the data leaves the machine
    at that point even though the SQLite file itself never does. Every
    tool's description (the text an LLM/agent actually sees) must carry
    this warning verbatim, not just the module's own docs, and this stays
    true automatically for any tool added later.
    """
    tools = (health_db.read_health_data, health_db.log_daily_metric, health_db.clear_metric)
    assert tools, "expected at least one tool to check"
    for tool in tools:
        assert health_db.CLOUD_MODEL_WARNING.strip() in tool.__doc__


def test_tool_annotations_reflect_read_write_behavior(health_db):
    """MCP tool annotations are client-facing hints about a tool's effects
    (readOnlyHint/destructiveHint/idempotentHint/openWorldHint) — clients
    can use these to, e.g., ask for confirmation before a destructive call.
    Assert they match what each tool actually does, not just that they're
    present, so a future behavior change can't silently leave stale hints.
    fastmcp's get_tool is async; there's no running event loop in a plain
    pytest test, so asyncio.run drives it here rather than pulling in
    pytest-asyncio for a single call site.
    """
    get_tool = lambda name: asyncio.run(health_db.mcp.get_tool(name))  # noqa: E731

    read_tool = get_tool("read_health_data")
    assert read_tool.annotations.readOnlyHint is True
    assert read_tool.annotations.openWorldHint is False

    log_tool = get_tool("log_daily_metric")
    assert log_tool.annotations.readOnlyHint is False
    assert log_tool.annotations.destructiveHint is False  # upserts, never drops data
    assert log_tool.annotations.idempotentHint is True

    clear_tool = get_tool("clear_metric")
    assert clear_tool.annotations.readOnlyHint is False
    assert clear_tool.annotations.destructiveHint is True  # blanks out a value
    assert clear_tool.annotations.idempotentHint is True
