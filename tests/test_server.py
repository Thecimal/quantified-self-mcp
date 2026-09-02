"""
Integration tests for the MCP tools in server.py.

Unlike test_logic.py, these need fastmcp installed (it's in
requirements.txt, and requirements-dev.txt pulls that in). Each test gets
its own throwaway database via the health_db fixture, so tests never
touch ./data/health.db or affect each other.
"""

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
