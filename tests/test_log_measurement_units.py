"""
Tests for unit validation in log_measurement (Issue #58 / QSM-AUDIT-002).
"""

import sqlite3
import sys
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logic import connect_writable, ensure_schema


def _fresh_server(tmp_path, monkeypatch):
    monkeypatch.setenv("HEALTH_DB_PATH", str(tmp_path / "health.db"))
    monkeypatch.setenv("HEALTH_PRIVATE_FIELDS", "")
    for name in ("server", "privacy", "tools.health", "tools.measurements", "tools.workouts"):
        sys.modules.pop(name, None)
    import server

    return server


@pytest.fixture
def srv(tmp_path, monkeypatch):
    s = _fresh_server(tmp_path, monkeypatch)
    with connect_writable(s.HEALTH_DB_PATH) as conn:
        ensure_schema(conn)
    return s


def test_conflicting_unit_weight_kg_raises_and_stores_nothing(srv):
    with pytest.raises(ToolError) as exc_info:
        srv.log_measurement(
            timestamp="2026-06-01T08:00:00",
            metric="weight_kg",
            value=154.3,
            unit="lb",
        )
    assert "[invalid_metric_value]" in str(exc_info.value)
    assert "kg" in str(exc_info.value)

    conn = sqlite3.connect(srv.HEALTH_DB_PATH)
    try:
        count = conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0]
        assert count == 0
    finally:
        conn.close()


def test_conflicting_unit_water_ml_raises_and_stores_nothing(srv):
    with pytest.raises(ToolError) as exc_info:
        srv.log_measurement(
            timestamp="2026-06-01T08:00:00",
            metric="water_ml",
            value=1.5,
            unit="L",
        )
    assert "[invalid_metric_value]" in str(exc_info.value)
    assert "mL" in str(exc_info.value)

    conn = sqlite3.connect(srv.HEALTH_DB_PATH)
    try:
        count = conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0]
        assert count == 0
    finally:
        conn.close()


def test_matching_unit_and_omitted_unit_are_accepted(srv):
    # unit omitted
    res1 = srv.log_measurement(
        timestamp="2026-06-01T08:00:00",
        metric="weight_kg",
        value=70.5,
    )
    assert res1.measurement.value == 70.5

    # unit matching lowercase
    res2 = srv.log_measurement(
        timestamp="2026-06-01T08:05:00",
        metric="weight_kg",
        value=70.6,
        unit="kg",
    )
    assert res2.measurement.value == 70.6
    assert res2.measurement.unit == "kg"

    # unit matching uppercase (case-insensitive)
    res3 = srv.log_measurement(
        timestamp="2026-06-01T08:10:00",
        metric="weight_kg",
        value=70.7,
        unit="KG",
    )
    assert res3.measurement.value == 70.7

    conn = sqlite3.connect(srv.HEALTH_DB_PATH)
    try:
        count = conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0]
        assert count == 3
    finally:
        conn.close()


def test_metric_without_registry_unit_logs_unchanged(srv):
    # mood has unit=None in registry
    res1 = srv.log_measurement(
        timestamp="2026-06-01T08:00:00",
        metric="mood",
        value=8,
    )
    assert res1.measurement.value == 8

    res2 = srv.log_measurement(
        timestamp="2026-06-01T08:05:00",
        metric="mood",
        value=7,
        unit="score",
    )
    assert res2.measurement.value == 7
    assert res2.measurement.unit == "score"
