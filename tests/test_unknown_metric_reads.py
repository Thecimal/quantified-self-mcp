"""A metric name nothing is stored under must not look like "no data".

read_measurements and get_metric_provenance used to answer a mistyped or unsupported metric (say "sleep"
instead of "sleep_hours") with a successful, empty result, indistinguishable from a supported metric that
simply has no readings in the window, so a client would tell the user they have no sleep data. Every other
metric-taking tool already rejects it with a coded error that lists the supported metrics; these two now do too.
"""

import sqlite3
import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("HEALTH_DB_PATH", str(tmp_path / "health.db"))
    monkeypatch.setenv("HEALTH_PRIVATE_FIELDS", "weight_kg")
    for name in ("server", "privacy", "tools.health", "tools.measurements", "tools.workouts"):
        sys.modules.pop(name, None)
    import server

    server.log_measurement(timestamp="2026-01-10T07:00:00", metric="steps", value=500, source="Phone")
    return server


def _counts(srv):
    with sqlite3.connect(srv.HEALTH_DB_PATH) as conn:
        return tuple(
            conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("measurements", "daily_metrics", "aggregation_rules")
        )


CALLS = {
    "read_measurements": lambda m: {"metric": m},
    "get_metric_provenance": lambda m: {"metric": m, "date": "2026-01-10"},
}


@pytest.mark.parametrize("tool", sorted(CALLS))
async def test_unsupported_metric_is_an_invalid_metric_error_over_mcp(srv, tool):
    before = _counts(srv)
    async with Client(srv.mcp) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool(tool, CALLS[tool]("sleep"))
    message = str(caught.value)
    assert message.startswith("[invalid_metric] "), message
    assert "'sleep'" in message
    assert "Supported metrics:" in message and "sleep_hours" in message and "steps" in message
    assert "Did you mean 'sleep_hours'?" in message
    assert "health.db" not in message and "SELECT" not in message  # no paths or SQL
    assert _counts(srv) == before  # a read never changes anything


@pytest.mark.parametrize("tool", sorted(CALLS))
async def test_supported_metric_without_readings_is_still_an_empty_success(srv, tool):
    async with Client(srv.mcp) as client:
        result = await client.call_tool(tool, CALLS[tool]("sleep_hours"))
    payload = result.structured_content
    assert payload.get("measurements", payload.get("sources")) == []


@pytest.mark.parametrize("tool", sorted(CALLS))
async def test_supported_metric_with_readings_is_unchanged(srv, tool):
    async with Client(srv.mcp) as client:
        result = await client.call_tool(tool, CALLS[tool]("steps"))
    payload = result.structured_content
    assert len(payload.get("measurements", payload.get("sources"))) == 1


@pytest.mark.parametrize("bad", ["", "   ", "STEPS", "step", "steps "])
async def test_nearby_invalid_names_are_rejected_not_empty(srv, bad):
    async with Client(srv.mcp) as client:
        with pytest.raises(ToolError, match=r"\[invalid_metric\]"):
            await client.call_tool("read_measurements", {"metric": bad})


def test_unknown_metric_without_a_close_match_gets_no_hint(srv):
    with pytest.raises(ToolError) as caught:
        srv.read_measurements(metric="zzzz")
    assert "Did you mean" not in str(caught.value)
    assert "Supported metrics:" in str(caught.value)


def test_metric_with_stored_readings_but_no_rule_is_still_readable(srv):
    # An aggregation rule can be removed after readings exist (nothing prevents it); those readings are real
    # data, so reading them back must keep working rather than be rejected as "unsupported".
    with sqlite3.connect(srv.HEALTH_DB_PATH) as conn:
        conn.execute("INSERT INTO aggregation_rules (metric, method) VALUES ('custom_x', 'sum')")
    srv.log_measurement(timestamp="2026-01-10T08:00:00", metric="custom_x", value=3, source="Phone")
    with sqlite3.connect(srv.HEALTH_DB_PATH) as conn:
        conn.execute("DELETE FROM aggregation_rules WHERE metric = 'custom_x'")
    assert [m.value for m in srv.read_measurements(metric="custom_x").measurements] == [3.0]
    assert srv.get_metric_provenance(metric="custom_x", date="2026-01-10").sources


def test_private_metric_is_still_refused_as_invalid_field(srv):
    with pytest.raises(ToolError, match=r"\[invalid_field\].*private"):
        srv.read_measurements(metric="weight_kg")
    with pytest.raises(ToolError, match=r"\[invalid_field\].*private"):
        srv.get_metric_provenance(metric="weight_kg", date="2026-01-10")


def test_omitting_the_metric_still_reads_everything(srv):
    assert srv.read_measurements().count == 1
