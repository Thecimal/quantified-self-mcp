"""
Tests for Oura Ring import adapter (import_adapters.py::adapt_oura).
"""

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from import_adapters import ADAPTERS, adapt_oura, detect_adapter
from init_db import init_health_db
from source_coverage import KNOWN_STATISTICS


def test_oura_adapter_parses_sleep_and_activity(tmp_path):
    records = [
        {
            "id": "sleep-1",
            "day": "2026-03-01",
            "bedtime_start": "2026-02-28T23:00:00+00:00",
            "bedtime_end": "2026-03-01T07:00:00+00:00",
            "total_sleep_duration": 28800,  # 8 hours
            "average_hrv": 58.5,
            "lowest_heart_rate": 52,
            "type": "long_sleep",
        },
        {
            "id": "act-1",
            "day": "2026-03-01",
            "steps": 11200,
        },
        {
            "id": "workout-1",
            "day": "2026-03-01",
            "activity": "cycling",
            "duration": 2700,  # 45 minutes
        },
    ]
    file_path = tmp_path / "oura.json"
    file_path.write_text(json.dumps(records), encoding="utf-8")

    res = adapt_oura(file_path)
    assert res.skipped == 0
    assert set(res.present_columns) == {"sleep_hours", "resting_heart_rate", "hrv_ms", "steps", "workout_minutes"}
    assert res.rows == [
        {
            "date": "2026-03-01",
            "steps": 11200,
            "sleep_hours": 8.0,
            "resting_heart_rate": 52,
            "hrv_ms": 58.5,
            "workout_minutes": 45,
        }
    ]

    # Check raw observations have source="Oura"
    assert all(o["source"] == "Oura" for o in res.raw_measurements)
    metrics = {o["metric"]: o["value"] for o in res.raw_measurements}
    assert metrics["sleep_hours"] == 8.0
    assert metrics["steps"] == 11200
    assert metrics["resting_heart_rate"] == 52
    assert metrics["hrv_ms"] == 58.5
    assert metrics["workout_minutes"] == 45


def test_oura_is_registered_and_its_hrv_is_known_to_be_rmssd(tmp_path):
    assert ADAPTERS["oura"] is adapt_oura
    assert KNOWN_STATISTICS[("oura", "hrv_ms")] == "rmssd"
    sample = tmp_path / "sample.json"
    sample.write_text("{}", encoding="utf-8")
    assert detect_adapter(sample) == "csv"  # JSON requires explicit --source oura


def test_oura_adapter_supports_data_wrapper(tmp_path):
    payload = {
        "data": [
            {
                "day": "2026-03-02",
                "steps": 8500,
            }
        ]
    }
    file_path = tmp_path / "oura_export.json"
    file_path.write_text(json.dumps(payload), encoding="utf-8")

    res = adapt_oura(file_path)
    assert len(res.rows) == 1
    assert res.rows[0]["steps"] == 8500


def test_oura_adapter_end_to_end_import(tmp_path):
    payload = [
        {
            "day": "2026-03-03",
            "total_sleep_duration": 25200,  # 7.0 h
            "average_hrv": 60.0,
            "lowest_heart_rate": 50,
            "steps": 9500,
        }
    ]
    json_path = tmp_path / "oura.json"
    json_path.write_text(json.dumps(payload), encoding="utf-8")

    db_path = tmp_path / "health.db"
    init_health_db(json_path, db_path, replace=False, source="oura")

    conn = sqlite3.connect(db_path)
    try:
        rows = dict(conn.execute("SELECT metric, value FROM daily_metrics WHERE date = '2026-03-03'").fetchall())
        assert rows["steps"] == 9500
        assert rows["sleep_hours"] == 7.0
        assert rows["resting_heart_rate"] == 50
        assert rows["hrv_ms"] == 60.0

        # Confirm provenance in measurements table
        measurements = conn.execute("SELECT DISTINCT source FROM measurements").fetchall()
        assert [m[0] for m in measurements] == ["Oura"]
    finally:
        conn.close()
