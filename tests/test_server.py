"""
Integration tests for the MCP tools in server.py.

Unlike test_logic.py, these need fastmcp installed (it's in
requirements.txt, and requirements-dev.txt pulls that in). Each test gets
its own throwaway database via the health_db fixture, so tests never
touch ./data/health.db or affect each other.
"""

import asyncio
import csv
import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ResourceError, ToolError

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
    logged = health_db.log_daily_metric(date="2026-01-01", steps=5000, mood=4)
    assert logged.logged == {"steps": 5000, "mood": 4}
    assert logged.row.steps == 5000
    assert logged.row.mood == 4

    read_back = health_db.read_health_data(start_date="2026-01-01", end_date="2026-01-01")
    assert read_back.rows[0].steps == 5000
    assert read_back.rows[0].mood == 4


def test_log_daily_metric_does_not_clear_other_fields(health_db):
    health_db.log_daily_metric(date="2026-01-02", steps=8000)
    health_db.log_daily_metric(date="2026-01-02", mood=5)
    row = health_db.log_daily_metric(date="2026-01-02", water_ml=2000).row
    assert row.steps == 8000
    assert row.mood == 5
    assert row.water_ml == 2000


def test_log_daily_metric_rejects_out_of_range_value(health_db):
    with pytest.raises(ToolError, match=r"\[invalid_metric_value\].*mood"):
        health_db.log_daily_metric(date="2026-01-03", mood=99)


def test_log_daily_metric_requires_at_least_one_metric(health_db):
    with pytest.raises(ToolError, match=r"\[missing_metric\]"):
        health_db.log_daily_metric(date="2026-01-04")


def test_log_daily_metric_rejects_bad_date(health_db):
    with pytest.raises(ToolError, match=r"\[invalid_date\]"):
        health_db.log_daily_metric(date="not-a-date", steps=1000)


def test_clear_metric_blanks_only_the_given_field(health_db):
    health_db.log_daily_metric(date="2026-01-05", steps=9000, mood=3)
    result = health_db.clear_metric(date="2026-01-05", field="mood")
    assert result.row.mood is None
    assert result.row.steps == 9000


def test_clear_metric_rejects_unknown_field(health_db):
    with pytest.raises(ToolError, match=r"\[invalid_field\]"):
        health_db.clear_metric(date="2026-01-06", field="not_a_real_field")


def test_clear_metric_on_a_date_with_no_row_reports_nothing_to_clear(health_db):
    result = health_db.clear_metric(date="2026-01-07", field="mood")
    assert result.row is None
    assert result.note is not None


def test_read_health_data_rejects_inverted_range(health_db):
    with pytest.raises(ToolError, match=r"\[invalid_range\]"):
        health_db.read_health_data(start_date="2026-02-01", end_date="2026-01-01")


def test_export_health_data_csv_writes_a_file_with_the_expected_rows(health_db):
    health_db.log_daily_metric(date="2026-01-01", steps=5000, mood=4)
    health_db.log_daily_metric(date="2026-01-02", steps=6000)

    result = health_db.export_health_data_csv(start_date="2026-01-01", end_date="2026-01-02")

    assert result.rows_exported == 2
    out_path = Path(result.path)
    assert out_path.exists()

    with out_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["date"] == "2026-01-01"
    assert rows[0]["steps"] == "5000"
    assert rows[0]["mood"] == "4"
    assert rows[1]["date"] == "2026-01-02"
    assert rows[1]["steps"] == "6000"
    assert rows[1]["mood"] == ""


def test_export_health_data_csv_redacts_private_fields(tmp_path, monkeypatch):
    db_path = tmp_path / "health.db"
    monkeypatch.setenv("HEALTH_DB_PATH", str(db_path))
    monkeypatch.setenv("HEALTH_PRIVATE_FIELDS", "mood")
    sys.modules.pop("server", None)
    import server as health_db

    health_db.log_daily_metric(date="2026-01-01", steps=5000, mood=4)
    result = health_db.export_health_data_csv(start_date="2026-01-01", end_date="2026-01-01")

    with Path(result.path).open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["steps"] == "5000"
    assert rows[0]["mood"] == ""


def test_export_health_data_csv_rejects_inverted_range(health_db):
    with pytest.raises(ToolError, match=r"\[invalid_range\]"):
        health_db.export_health_data_csv(start_date="2026-02-01", end_date="2026-01-01")


def test_database_locked_error_is_distinguished_from_generic_database_error(health_db, monkeypatch):
    """A concurrent-writer lock is retry-worthy; a missing/corrupt database
    isn't. Both used to surface as the same generic message — assert the
    error code actually tracks which failure occurred, using the real
    sqlite3.OperationalError('database is locked') a busy connection would
    raise, not a stand-in exception.
    """
    import sqlite3

    def _raise_locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(health_db, "connect_writable", _raise_locked)
    with pytest.raises(ToolError, match=r"\[database_locked\]"):
        health_db.log_daily_metric(date="2026-01-08", steps=1000)


def test_every_tool_carries_the_cloud_model_warning(health_db):
    """Whatever a tool returns is sent to whichever model the MCP client is
    configured with — if that's a cloud model, the data leaves the machine
    at that point even though the SQLite file itself never does. Every
    tool's description (the text an LLM/agent actually sees) must carry
    this warning verbatim, not just the module's own docs, and this stays
    true automatically for any tool added later.
    """
    tools = (
        health_db.read_health_data,
        health_db.log_daily_metric,
        health_db.clear_metric,
        health_db.export_health_data_csv,
    )
    assert tools, "expected at least one tool to check"
    for tool in tools:
        assert health_db.CLOUD_MODEL_WARNING.strip() in tool.__doc__


def test_metrics_schema_reflects_bounds_and_privacy(tmp_path, monkeypatch):
    server = _import_server_with_private_fields(tmp_path, monkeypatch, "weight_kg")
    schema = {entry["name"]: entry for entry in server.metrics_schema()}
    assert schema["steps"] == {"name": "steps", "min": 0, "max": 200_000, "label": "steps", "private": False}
    assert schema["weight_kg"]["private"] is True
    assert set(schema) == set(server.METRIC_COLUMNS)


def test_day_snapshot_returns_redacted_data_for_a_logged_day(health_db):
    health_db.log_daily_metric(date="2026-01-12", steps=7000, mood=6)
    snapshot = health_db.day_snapshot("2026-01-12")
    assert snapshot["date"] == "2026-01-12"
    assert snapshot["steps"] == 7000
    assert snapshot["mood"] == 6


def test_day_snapshot_returns_all_nulls_for_a_day_with_no_data(health_db):
    snapshot = health_db.day_snapshot("2026-01-13")
    assert snapshot["date"] == "2026-01-13"
    assert all(snapshot[m] is None for m in health_db.METRIC_COLUMNS)


def test_day_snapshot_rejects_bad_date(health_db):
    with pytest.raises(ResourceError):
        health_db.day_snapshot("not-a-date")


def test_resources_are_registered_and_readable_over_the_wire(health_db):
    """Confirms both resources actually reach an MCP client: they're listed
    (as a plain resource and a template, respectively) and reading them
    returns valid JSON — this is also a regression test for a real bug
    caught during development, where returning a Pydantic model directly
    from a resource function crashed at read time because fastmcp's
    resource path (unlike the tool path) doesn't serialize BaseModel
    instances on its own.
    """

    async def _check():
        async with Client(health_db.mcp) as client:
            resources = await client.list_resources()
            assert str(resources[0].uri) == "health://metrics/schema"

            templates = await client.list_resource_templates()
            assert templates[0].uri_template == "health://day/{date}"

            schema_result = await client.read_resource("health://metrics/schema")
            schema = json.loads(schema_result[0].text)
            assert any(entry["name"] == "steps" for entry in schema)

            day_result = await client.read_resource("health://day/2026-01-14")
            day = json.loads(day_result[0].text)
            assert day["date"] == "2026-01-14"

    asyncio.run(_check())


def test_log_daily_metric_has_a_structured_output_schema_on_the_wire(health_db):
    """Direct calls (used above) bypass FastMCP's protocol layer entirely,
    so they can't confirm output_schema/structured_content actually reach
    a client. Drive one real call through an in-memory fastmcp Client and
    check the wire-level result: structured_content should be the typed
    object itself (matching LogDailyMetricResult), not the previous
    behavior of a JSON string wrapped in {"result": ...}.
    """
    from fastmcp import Client

    async def _call():
        async with Client(health_db.mcp) as client:
            return await client.call_tool("log_daily_metric", {"date": "2026-01-09", "steps": 4200})

    result = asyncio.run(_call())
    assert result.structured_content["logged"] == {"steps": 4200}
    assert result.structured_content["row"]["steps"] == 4200
    assert result.structured_content["row"]["date"] == "2026-01-09"

    tool = asyncio.run(health_db.mcp.get_tool("log_daily_metric"))
    assert tool.output_schema is not None
    assert tool.output_schema.get("properties", {}).keys() >= {"logged", "row"}


def _import_server_with_private_fields(tmp_path, monkeypatch, private_fields):
    """Like the health_db fixture, but also sets HEALTH_PRIVATE_FIELDS
    before import, since PRIVATE_FIELDS is parsed once at module import
    time. Not a fixture itself since most tests don't need this env var.
    """
    db_path = tmp_path / "health.db"
    monkeypatch.setenv("HEALTH_DB_PATH", str(db_path))
    monkeypatch.setenv("HEALTH_PRIVATE_FIELDS", private_fields)
    sys.modules.pop("server", None)
    import server

    return server


def test_read_health_data_redacts_private_fields(tmp_path, monkeypatch):
    server = _import_server_with_private_fields(tmp_path, monkeypatch, "weight_kg,mood")
    server.log_daily_metric(date="2026-01-10", steps=1000, weight_kg=70.5, mood=8)

    result = server.read_health_data(start_date="2026-01-10", end_date="2026-01-10")
    row = result.rows[0]
    assert row.steps == 1000  # not private, visible as normal
    assert row.weight_kg is None  # private, redacted regardless of what's stored
    assert row.mood is None  # private, redacted regardless of what's stored
    assert result.summary.steps.avg == 1000
    assert result.summary.weight_kg.avg is None
    assert result.summary.mood.max is None


def test_log_daily_metric_redacts_private_field_in_echoed_row(tmp_path, monkeypatch):
    server = _import_server_with_private_fields(tmp_path, monkeypatch, "weight_kg")
    result = server.log_daily_metric(date="2026-01-11", weight_kg=80.0)
    assert result.logged == {"weight_kg": 80.0}  # the caller's own input, already known to it
    assert result.row.weight_kg is None  # but the current-state row still redacts it


def test_unknown_private_field_name_is_ignored_not_fatal(tmp_path, monkeypatch):
    server = _import_server_with_private_fields(tmp_path, monkeypatch, "not_a_real_field, steps")
    assert server.PRIVATE_FIELDS == frozenset({"steps"})


def test_server_tools_work_end_to_end_against_an_encrypted_database(tmp_path, monkeypatch):
    """The full tool pipeline (log_daily_metric -> read_health_data ->
    clear_metric), not just logic.py's own connect_writable/
    readonly_connection, actually works with HEALTH_DB_PASSPHRASE set —
    this is what would have broken if e.g. one of server.py's `except
    sqlite3.Error` sites hadn't been updated to logic.db_error_types().
    """
    pytest.importorskip("sqlcipher3")
    db_path = tmp_path / "health.db"
    monkeypatch.setenv("HEALTH_DB_PATH", str(db_path))
    monkeypatch.setenv("HEALTH_DB_PASSPHRASE", "correct horse battery staple")
    sys.modules.pop("server", None)
    import server

    logged = server.log_daily_metric(date="2026-01-20", steps=6000)
    assert logged.row.steps == 6000

    read_back = server.read_health_data(start_date="2026-01-20", end_date="2026-01-20")
    assert read_back.rows[0].steps == 6000

    cleared = server.clear_metric(date="2026-01-20", field="steps")
    assert cleared.row.steps is None

    # And, as in test_logic.py's version of this check: genuinely
    # encrypted, not just opened through a different driver.
    import sqlite3

    monkeypatch.delenv("HEALTH_DB_PASSPHRASE", raising=False)
    plain_conn = sqlite3.connect(str(db_path))
    with pytest.raises(sqlite3.DatabaseError):
        plain_conn.execute("SELECT * FROM daily_metrics").fetchall()
    plain_conn.close()


def test_tool_annotations_reflect_read_write_behavior(health_db):
    """MCP tool annotations are client-facing hints about a tool's effects
    (read_only_hint/destructive_hint/idempotent_hint/open_world_hint) —
    clients can use these to, e.g., ask for confirmation before a
    destructive call. Assert they match what each tool actually does, not
    just that they're present, so a future behavior change can't silently
    leave stale hints.
    fastmcp's get_tool is async; there's no running event loop in a plain
    pytest test, so asyncio.run drives it here rather than pulling in
    pytest-asyncio for a single call site.
    """
    get_tool = lambda name: asyncio.run(health_db.mcp.get_tool(name))  # noqa: E731

    read_tool = get_tool("read_health_data")
    assert read_tool.annotations.read_only_hint is True
    assert read_tool.annotations.open_world_hint is False

    log_tool = get_tool("log_daily_metric")
    assert log_tool.annotations.read_only_hint is False
    assert log_tool.annotations.destructive_hint is False  # upserts, never drops data
    assert log_tool.annotations.idempotent_hint is True

    clear_tool = get_tool("clear_metric")
    assert clear_tool.annotations.read_only_hint is False
    assert clear_tool.annotations.destructive_hint is True  # blanks out a value
    assert clear_tool.annotations.idempotent_hint is True
