"""Tests for Google Fit Takeout import adapter (import_adapters.py::adapt_google_fit)."""

import json
import sqlite3
import sys
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from import_adapters import ADAPTERS, RowError, adapt_google_fit, adapt_health_connect
from init_db import init_health_db


def _ns(iso_utc: str) -> str:
    dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    return str(int(dt.timestamp() * 1_000_000_000))


def _local_day(iso_utc: str) -> str:
    dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).astimezone()
    return dt.date().isoformat()


def test_google_fit_is_registered():
    assert ADAPTERS["google-fit"] is adapt_google_fit


def test_google_fit_daily_activity_metrics_csv(tmp_path):
    csv_path = tmp_path / "Daily activity metrics.csv"
    csv_path.write_text(
        "Date,Move Minutes count,Calories (kcal),Distance (m),Step count,Average weight (kg)\n"
        "2026-03-01,45,2100.5,6400.0,8500,72.4\n"
        "2026-03-02,30,1950.0,4800.0,6200,\n",
        encoding="utf-8",
    )

    res = adapt_google_fit(csv_path)
    assert res.skipped == 0
    assert set(res.present_columns) == {"steps", "weight_kg", "workout_minutes"}
    assert res.rows == [
        {"date": "2026-03-01", "steps": 8500, "weight_kg": 72.4, "workout_minutes": 45},
        {"date": "2026-03-02", "steps": 6200, "workout_minutes": 30},
    ]
    assert all(o["source"] == "Google Fit" for o in res.raw_measurements)


def test_google_fit_all_data_json(tmp_path):
    t1 = "2026-03-05T10:00:00Z"
    t2 = "2026-03-05T11:00:00Z"
    sleep_start = "2026-03-05T01:00:00Z"
    sleep_mid = "2026-03-05T07:00:00Z"
    sleep_end = "2026-03-05T07:30:00Z"
    expected_day = _local_day(t1)

    payload = {
        "Data Points": [
            {
                "startTimeNanos": _ns(t1),
                "endTimeNanos": _ns(t2),
                "dataTypeName": "com.google.step_count.delta",
                "value": [{"intVal": 4200}],
            },
            {
                "startTimeNanos": _ns(t1),
                "endTimeNanos": _ns(t2),
                "dataTypeName": "com.google.heart_rate.bpm",
                "fitValue": [{"value": {"fpVal": 72.0}}],
            },
            {
                "startTimeNanos": _ns(t1),
                "endTimeNanos": _ns(t1),
                "dataTypeName": "com.google.weight",
                "value": [{"fpVal": 71.8}],
            },
            {
                "startTimeNanos": _ns(sleep_start),
                "endTimeNanos": _ns(sleep_mid),
                "dataTypeName": "com.google.sleep.segment",
                "value": [{"intVal": 4}],  # Light sleep -> counted
            },
            {
                "startTimeNanos": _ns(sleep_mid),
                "endTimeNanos": _ns(sleep_end),
                "dataTypeName": "com.google.sleep.segment",
                "value": [{"intVal": 1}],  # Awake -> excluded
            },
            {
                "startTimeNanos": _ns(t1),
                "endTimeNanos": _ns(t1),
                "dataTypeName": "com.google.hydration",
                "value": [{"fpVal": 0.5}],  # 500 mL
            },
            {
                "startTimeNanos": _ns(t1),
                "endTimeNanos": _ns(t2),
                "dataTypeName": "com.google.calories.expended",
                "value": [{"fpVal": 120.0}],
            },
        ]
    }
    json_path = tmp_path / "all_data.json"
    json_path.write_text(json.dumps(payload), encoding="utf-8")

    res = adapt_google_fit(json_path)
    assert res.skipped == 0
    assert res.unsupported_types == {"com.google.calories.expended": 1}
    assert len(res.rows) == 1
    row = res.rows[0]
    assert row["date"] == expected_day
    assert row["steps"] == 4200
    assert row["heart_rate"] == 72
    assert row["weight_kg"] == 71.8
    assert row["sleep_hours"] == 6.0
    assert row["water_ml"] == 500


def test_google_fit_sessions_json_and_skips_still(tmp_path):
    s_dir = tmp_path / "All Sessions"
    s_dir.mkdir()
    (s_dir / "run.json").write_text(
        json.dumps(
            {
                "fitnessActivity": "running",
                "startTime": "2026-03-06T08:00:00+00:00",
                "endTime": "2026-03-06T08:45:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    (s_dir / "still.json").write_text(
        json.dumps(
            {
                "fitnessActivity": "still",
                "startTime": "2026-03-06T09:00:00+00:00",
                "endTime": "2026-03-06T10:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    res = adapt_google_fit(s_dir)
    assert res.skipped == 0
    assert res.rows == [{"date": _local_day("2026-03-06T08:00:00+00:00"), "workout_minutes": 45}]


def test_google_fit_takeout_zip_avoids_double_counting_and_imports_to_db(tmp_path):
    t1 = "2026-03-10T12:00:00Z"
    t2 = "2026-03-10T12:30:00Z"
    day = _local_day(t1)

    zip_path = tmp_path / "takeout-google-fit.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        # All Data has 5000 steps on day
        zf.writestr(
            "Takeout/Fit/All Data/derived_com.google.step_count.delta.json",
            json.dumps(
                {
                    "Data Points": [
                        {
                            "startTimeNanos": _ns(t1),
                            "endTimeNanos": _ns(t2),
                            "dataTypeName": "com.google.step_count.delta",
                            "value": [{"intVal": 5000}],
                        }
                    ]
                }
            ),
        )
        # Daily activity metrics.csv also has 5000 steps on day (must NOT double-count to 10000!)
        # plus 2026-03-11 which is only in the CSV.
        zf.writestr(
            "Takeout/Fit/Daily activity metrics/Daily activity metrics.csv",
            f"Date,Step count,Move Minutes count\n{day},5000,20\n2026-03-11,7100,35\n",
        )

    db_path = tmp_path / "health.db"
    init_health_db(zip_path, db_path, replace=False, source="google-fit")

    conn = sqlite3.connect(db_path)
    try:
        day_rows = dict(conn.execute("SELECT metric, value FROM daily_metrics WHERE date = ?", (day,)).fetchall())
        assert day_rows["steps"] == 5000  # not 10000
        assert day_rows["workout_minutes"] == 20

        next_rows = dict(conn.execute("SELECT metric, value FROM daily_metrics WHERE date = '2026-03-11'").fetchall())
        assert next_rows["steps"] == 7100
        assert next_rows["workout_minutes"] == 35

        sources = [r[0] for r in conn.execute("SELECT DISTINCT source FROM measurements").fetchall()]
        assert sources == ["Google Fit"]
    finally:
        conn.close()


def test_google_fit_skips_malformed_points_and_rejects_empty_dir(tmp_path):
    with pytest.raises(RowError):
        adapt_google_fit(tmp_path)

    bad_json = tmp_path / "bad.json"
    bad_json.write_text(
        json.dumps(
            {
                "Data Points": [
                    {
                        "startTimeNanos": "not-a-number",
                        "dataTypeName": "com.google.step_count.delta",
                        "value": [{"intVal": 10}],
                    },
                    {
                        "startTimeNanos": _ns("2026-03-12T12:00:00Z"),
                        "dataTypeName": "com.google.step_count.delta",
                        "value": [{"intVal": 1500}],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    res = adapt_google_fit(bad_json)
    assert res.skipped == 1
    assert len(res.rows) == 1
    assert res.rows[0]["steps"] == 1500


def test_health_connect_zip_with_json_and_backup_db_error(tmp_path):
    hc_zip = tmp_path / "hc.zip"
    with zipfile.ZipFile(hc_zip, "w") as zf:
        zf.writestr(
            "records.json",
            json.dumps(
                [
                    {
                        "recordType": "StepsRecord",
                        "startTime": "2026-03-15T09:00:00Z",
                        "endTime": "2026-03-15T09:30:00Z",
                        "count": 1200,
                    }
                ]
            ),
        )
    res = adapt_health_connect(hc_zip)
    assert res.rows == [{"date": "2026-03-15", "steps": 1200}]

    backup_zip = tmp_path / "backup.zip"
    with zipfile.ZipFile(backup_zip, "w") as zf:
        zf.writestr("health_connect_export.db", b"SQLite format 3\x00")
    with pytest.raises(SystemExit):
        adapt_health_connect(backup_zip)
