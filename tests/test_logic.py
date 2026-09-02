"""
Unit tests for logic.py.

Run with: pytest

These only exercise the framework-free helpers in logic.py, so they run
without fastmcp installed — server.py itself is not imported here.
"""

import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logic import (
    ADDED_COLUMNS,
    ensure_schema,
    numeric_stats,
    parse_date,
    resolve_range,
    upsert_metrics,
    validate_metrics,
)  # noqa: E402


def test_parse_date_valid():
    assert parse_date("2026-08-23", "start_date") == date(2026, 8, 23)


def test_parse_date_rejects_wrong_format():
    with pytest.raises(ValueError, match="start_date must be formatted YYYY-MM-DD"):
        parse_date("08/23/2026", "start_date")


def test_resolve_range_defaults_to_today_and_default_days():
    start, end = resolve_range(None, None, default_days=30)
    assert end == date.today()
    assert (end - start).days == 30


def test_resolve_range_explicit_dates():
    start, end = resolve_range("2026-01-01", "2026-01-31", default_days=30)
    assert start == date(2026, 1, 1)
    assert end == date(2026, 1, 31)


def test_resolve_range_rejects_start_after_end():
    with pytest.raises(ValueError, match="is after end_date"):
        resolve_range("2026-02-01", "2026-01-01", default_days=30)


def test_resolve_range_rejects_absurdly_wide_range():
    with pytest.raises(ValueError, match="over the"):
        resolve_range("2000-01-01", "2026-01-01", default_days=30)


def test_numeric_stats_empty():
    assert numeric_stats([], "steps") == {"avg": None, "min": None, "max": None}


def test_numeric_stats_ignores_none_but_keeps_zero():
    rows = [{"steps": 0}, {"steps": None}, {"steps": 10000}]
    assert numeric_stats(rows, "steps") == {"avg": 5000.0, "min": 0, "max": 10000}


def test_ensure_schema_creates_table_with_all_columns():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(daily_metrics)")}
    assert columns == {
        "date",
        "steps",
        "sleep_hours",
        "resting_heart_rate",
        *ADDED_COLUMNS,
    }


def test_ensure_schema_migrates_older_table_missing_new_columns():
    conn = sqlite3.connect(":memory:")
    # Simulate a database created before weight/workout/mood/water existed.
    conn.executescript(
        """
        CREATE TABLE daily_metrics (
            date TEXT PRIMARY KEY,
            steps INTEGER,
            sleep_hours REAL,
            resting_heart_rate INTEGER
        );
        """
    )
    conn.execute(
        "INSERT INTO daily_metrics (date, steps, sleep_hours, resting_heart_rate) VALUES (?, ?, ?, ?)",
        ("2026-01-01", 5000, 7.0, 60),
    )
    conn.commit()

    ensure_schema(conn)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(daily_metrics)")}
    assert set(ADDED_COLUMNS).issubset(columns)
    # Pre-existing row survives the migration, with new columns defaulting to NULL.
    row = conn.execute("SELECT steps, weight_kg FROM daily_metrics WHERE date = '2026-01-01'").fetchone()
    assert row == (5000, None)


def _conn_with_schema():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    return conn


def test_upsert_metrics_inserts_new_row():
    conn = _conn_with_schema()
    upsert_metrics(conn, [{"date": "2026-08-01", "steps": 8000, "mood": 4}])
    row = conn.execute("SELECT steps, mood, weight_kg FROM daily_metrics WHERE date = '2026-08-01'").fetchone()
    assert row == (8000, 4, None)


def test_upsert_metrics_leaves_unmentioned_columns_untouched():
    conn = _conn_with_schema()
    upsert_metrics(conn, [{"date": "2026-08-01", "steps": 8000, "mood": 4}])
    # A second upsert for the same date, only setting a different column.
    upsert_metrics(conn, [{"date": "2026-08-01", "weight_kg": 70.5}])
    row = conn.execute("SELECT steps, mood, weight_kg FROM daily_metrics WHERE date = '2026-08-01'").fetchone()
    assert row == (8000, 4, 70.5)


def test_upsert_metrics_handles_rows_with_different_column_sets_in_one_call():
    conn = _conn_with_schema()
    upsert_metrics(
        conn,
        [
            {"date": "2026-08-01", "steps": 8000},
            {"date": "2026-08-02", "mood": 3, "water_ml": 2000},
        ],
    )
    rows = {
        r[0]: r[1:]
        for r in conn.execute("SELECT date, steps, mood, water_ml FROM daily_metrics ORDER BY date")
    }
    assert rows["2026-08-01"] == (8000, None, None)
    assert rows["2026-08-02"] == (None, 3, 2000)


def test_upsert_metrics_rejects_row_without_date():
    conn = _conn_with_schema()
    with pytest.raises(ValueError):
        upsert_metrics(conn, [{"steps": 1000}])


def test_validate_metrics_accepts_in_range_values():
    validate_metrics({"steps": 10000, "mood": 5, "weight_kg": 70.0})  # should not raise


def test_validate_metrics_ignores_none_values():
    validate_metrics({"steps": None, "mood": None})  # should not raise


def test_validate_metrics_ignores_unrecognized_keys():
    validate_metrics({"not_a_real_metric": 999999})  # should not raise


def test_validate_metrics_rejects_out_of_range_value():
    with pytest.raises(ValueError, match="mood"):
        validate_metrics({"mood": 99})


def test_validate_metrics_rejects_negative_where_not_allowed():
    with pytest.raises(ValueError, match="resting_heart_rate"):
        validate_metrics({"resting_heart_rate": -5})
