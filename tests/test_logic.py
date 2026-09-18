"""
Unit tests for logic.py.

Run with: pytest

These only exercise the framework-free helpers in logic.py, so they run
without fastmcp installed — server.py itself is not imported here.
"""

import logging
import sqlite3
import sys
import threading
import time
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logic import (
    ADDED_COLUMNS,
    BUSY_TIMEOUT_MS,
    DAILY_LOG_SOURCE,
    DB_PASSPHRASE_ENV,
    MIGRATIONS,
    SCHEMA_VERSION,
    V5_ADDED_COLUMNS,
    WORKOUT_INTENSITIES,
    aggregate_measurements_to_daily,
    bulk_import_measurements,
    clear_daily_metric,
    connect_writable,
    daily_metrics_wide,
    db_error_types,
    default_data_dir,
    encryption_available,
    ensure_schema,
    get_metric_provenance,
    insert_measurement,
    insert_workout_session,
    measurement_rows_from_daily,
    numeric_stats,
    parse_date,
    query_measurements,
    query_workout_sessions,
    readonly_connection,
    resolve_range,
    resolve_source_conflicts,
    row_class,
    upsert_daily_metric_measurements,
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


def test_ensure_schema_creates_narrow_daily_metrics_projection():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(daily_metrics)")}
    assert columns == {"date", "metric", "value", "raw_measurement_count", "aggregation_method", "aggregated_at"}


def test_ensure_schema_seeds_aggregation_rules_for_every_known_metric():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    rules = dict(conn.execute("SELECT metric, method FROM aggregation_rules"))
    assert rules == {
        "steps": "sum",
        "sleep_hours": "sum",
        "resting_heart_rate": "mean",
        "water_ml": "sum",
        "workout_minutes": "sum",
        "hrv_ms": "mean",
        "heart_rate": "mean",
        "mood": "mean",
        "weight_kg": "last",
    }


def test_ensure_schema_migrates_a_populated_wide_table_into_measurements():
    """A database from before the v7 migration has real data sitting
    directly in the old wide daily_metrics table (one column per metric,
    written by the old upsert_metrics) — ensure_schema should carry that
    data forward into the new projection rather than silently dropping
    it, by synthesizing a measurements row for each populated cell (see
    _migrate_v7_project_daily_metrics_from_measurements).
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        f"""
        CREATE TABLE daily_metrics (
            date TEXT PRIMARY KEY,
            steps INTEGER,
            sleep_hours REAL,
            resting_heart_rate INTEGER,
            {", ".join(f"{name} {sqltype}" for name, sqltype in ADDED_COLUMNS.items())},
            {", ".join(f"{name} {sqltype}" for name, sqltype in V5_ADDED_COLUMNS.items())}
        );
        """
    )
    conn.execute(
        "INSERT INTO daily_metrics (date, steps, mood) VALUES (?, ?, ?)",
        ("2026-01-01", 8000, 4),
    )
    conn.commit()

    ensure_schema(conn)

    rows = daily_metrics_wide(conn, ["steps", "mood", "sleep_hours"], "2026-01-01", "2026-01-01")
    assert rows == [{"date": "2026-01-01", "steps": 8000, "mood": 4, "sleep_hours": None}]
    # And the old table is preserved, not dropped, in case it needs auditing.
    legacy = conn.execute("SELECT date, steps, mood FROM daily_metrics_legacy_v6").fetchall()
    assert legacy == [("2026-01-01", 8000, 4)]


def test_ensure_schema_migrates_an_empty_wide_table_cleanly():
    # The common path: a brand-new database walks the full migration
    # history (v1 creates the empty wide table, v7 converts it) with
    # nothing to carry forward.
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    assert conn.execute("SELECT COUNT(*) FROM daily_metrics").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM daily_metrics_legacy_v6").fetchone()[0] == 0


def test_ensure_schema_sets_user_version_to_latest():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_ensure_schema_only_runs_each_migration_once():
    """A second call shouldn't re-run any migration — mainly a guard
    against a future migration that (unlike most of the current ones)
    *isn't* naturally idempotent, since user_version having already
    advanced is the only thing that would stop it running again.
    """
    calls = []
    conn = sqlite3.connect(":memory:")
    patched = [
        (version, description, lambda c, v=version, m=migrate: (calls.append(v), m(c))[1])
        for version, description, migrate in MIGRATIONS
    ]
    original_migrations = MIGRATIONS[:]
    MIGRATIONS[:] = patched
    try:
        ensure_schema(conn)
        assert calls == [version for version, _, _ in original_migrations]
        ensure_schema(conn)
        assert calls == [version for version, _, _ in original_migrations]  # unchanged — nothing re-ran
    finally:
        MIGRATIONS[:] = original_migrations


def test_ensure_schema_heals_version_for_a_pre_versioning_database():
    """A database written by a release of this project from before schema
    versioning existed has every column already, but user_version is
    still SQLite's default of 0 (nothing ever set it). ensure_schema
    should recognize the schema is actually current and "heal" the
    version stamp, without erroring or duplicating any column — and
    still carry any data in the wide table forward via v7, same as any
    other pre-v7 database.
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        f"""
        CREATE TABLE daily_metrics (
            date TEXT PRIMARY KEY,
            steps INTEGER,
            sleep_hours REAL,
            resting_heart_rate INTEGER,
            {", ".join(f"{name} {sqltype}" for name, sqltype in ADDED_COLUMNS.items())},
            {", ".join(f"{name} {sqltype}" for name, sqltype in V5_ADDED_COLUMNS.items())}
        );
        """
    )
    conn.commit()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0

    ensure_schema(conn)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    columns = {row[1] for row in conn.execute("PRAGMA table_info(daily_metrics)")}
    assert columns == {"date", "metric", "value", "raw_measurement_count", "aggregation_method", "aggregated_at"}


def test_ensure_schema_does_not_touch_a_database_from_a_newer_version(caplog):
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()

    with caplog.at_level(logging.WARNING, logger="quantified-self-mcp"):
        ensure_schema(conn)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION + 1
    assert "newer than this version" in caplog.text


def _conn_with_schema():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    return conn


def test_upsert_daily_metric_measurements_inserts_new_row():
    conn = _conn_with_schema()
    upsert_daily_metric_measurements(conn, "2026-08-01", {"steps": 8000, "mood": 4})
    rows = daily_metrics_wide(conn, ["steps", "mood", "weight_kg"], "2026-08-01", "2026-08-01")
    assert rows == [{"date": "2026-08-01", "steps": 8000, "mood": 4, "weight_kg": None}]


def test_upsert_daily_metric_measurements_leaves_unmentioned_metrics_untouched():
    conn = _conn_with_schema()
    upsert_daily_metric_measurements(conn, "2026-08-01", {"steps": 8000, "mood": 4})
    # A second call for the same date, only setting a different metric.
    upsert_daily_metric_measurements(conn, "2026-08-01", {"weight_kg": 70.5})
    rows = daily_metrics_wide(conn, ["steps", "mood", "weight_kg"], "2026-08-01", "2026-08-01")
    assert rows == [{"date": "2026-08-01", "steps": 8000, "mood": 4, "weight_kg": 70.5}]


def test_upsert_daily_metric_measurements_overwrites_rather_than_accumulates():
    # Unlike log_measurement, this represents a single declared total —
    # calling it twice for the same (date, metric) replaces, not sums.
    conn = _conn_with_schema()
    upsert_daily_metric_measurements(conn, "2026-08-01", {"steps": 8000})
    upsert_daily_metric_measurements(conn, "2026-08-01", {"steps": 9000})
    rows = daily_metrics_wide(conn, ["steps"], "2026-08-01", "2026-08-01")
    assert rows == [{"date": "2026-08-01", "steps": 9000}]


def test_upsert_daily_metric_measurements_handles_multiple_dates_independently():
    conn = _conn_with_schema()
    upsert_daily_metric_measurements(conn, "2026-08-01", {"steps": 8000})
    upsert_daily_metric_measurements(conn, "2026-08-02", {"mood": 3, "water_ml": 2000})
    rows = {r["date"]: r for r in daily_metrics_wide(conn, ["steps", "mood", "water_ml"])}
    assert rows["2026-08-01"] == {"date": "2026-08-01", "steps": 8000, "mood": None, "water_ml": None}
    assert rows["2026-08-02"] == {"date": "2026-08-02", "steps": None, "mood": 3, "water_ml": 2000}


def test_upsert_daily_metric_measurements_is_a_noop_for_an_empty_dict():
    conn = _conn_with_schema()
    upsert_daily_metric_measurements(conn, "2026-08-01", {})  # should not raise
    assert daily_metrics_wide(conn, ["steps"], "2026-08-01", "2026-08-01") == []


def test_clear_daily_metric_removes_only_the_targeted_metric():
    conn = _conn_with_schema()
    upsert_daily_metric_measurements(conn, "2026-08-01", {"steps": 8000, "mood": 4})
    deleted = clear_daily_metric(conn, "2026-08-01", "steps")
    assert deleted == 1
    rows = daily_metrics_wide(conn, ["steps", "mood"], "2026-08-01", "2026-08-01")
    assert rows == [{"date": "2026-08-01", "steps": None, "mood": 4}]


def test_clear_daily_metric_removes_the_row_entirely_when_it_was_the_last_metric():
    conn = _conn_with_schema()
    upsert_daily_metric_measurements(conn, "2026-08-01", {"steps": 8000})
    clear_daily_metric(conn, "2026-08-01", "steps")
    assert daily_metrics_wide(conn, ["steps"], "2026-08-01", "2026-08-01") == []


def test_clear_daily_metric_clears_granular_log_measurement_readings_too():
    # Broader than the old "UPDATE ... SET field = NULL": everything
    # behind that day's value for that metric is cleared, not just a
    # value upsert_daily_metric_measurements wrote directly.
    conn = _conn_with_schema()
    insert_measurement(conn, "2026-08-01T07:00:00", "steps", 3000)
    insert_measurement(conn, "2026-08-01T19:00:00", "steps", 4000)
    deleted = clear_daily_metric(conn, "2026-08-01", "steps")
    assert deleted == 2
    assert daily_metrics_wide(conn, ["steps"], "2026-08-01", "2026-08-01") == []


def test_clear_daily_metric_returns_zero_when_nothing_to_clear():
    conn = _conn_with_schema()
    assert clear_daily_metric(conn, "2026-08-01", "steps") == 0


def test_daily_metrics_wide_omits_dates_with_none_of_the_requested_metrics():
    conn = _conn_with_schema()
    upsert_daily_metric_measurements(conn, "2026-08-01", {"water_ml": 2000})
    assert daily_metrics_wide(conn, ["steps", "mood"], "2026-08-01", "2026-08-01") == []


def test_daily_metrics_wide_respects_start_and_end_bounds():
    conn = _conn_with_schema()
    upsert_daily_metric_measurements(conn, "2026-08-01", {"steps": 1})
    upsert_daily_metric_measurements(conn, "2026-08-05", {"steps": 2})
    upsert_daily_metric_measurements(conn, "2026-08-10", {"steps": 3})
    rows = daily_metrics_wide(conn, ["steps"], "2026-08-02", "2026-08-09")
    assert [r["date"] for r in rows] == ["2026-08-05"]


def test_daily_metrics_wide_requires_at_least_one_metric():
    conn = _conn_with_schema()
    with pytest.raises(ValueError):
        daily_metrics_wide(conn, [])


def test_measurement_rows_from_daily_flattens_wide_rows():
    rows = measurement_rows_from_daily([{"date": "2026-08-01", "steps": 8000, "mood": None, "water_ml": 2000}])
    assert {(r["metric"], r["value"]) for r in rows} == {("steps", 8000), ("water_ml", 2000)}
    assert all(r["timestamp"] == "2026-08-01T12:00:00" for r in rows)


def test_measurement_rows_from_daily_honors_skip_metrics():
    rows = measurement_rows_from_daily(
        [{"date": "2026-08-01", "steps": 8000, "sleep_hours": 7.5}], skip_metrics=frozenset({"steps"})
    )
    assert [r["metric"] for r in rows] == ["sleep_hours"]


def test_bulk_import_measurements_populates_the_projection():
    conn = _conn_with_schema()
    result = bulk_import_measurements(
        conn,
        "csv",
        [
            {"timestamp": "2026-08-01T12:00:00", "metric": "steps", "value": 8000},
            {"timestamp": "2026-08-01T12:00:00", "metric": "mood", "value": 4},
        ],
    )
    assert result["status"] == "ok"
    rows = daily_metrics_wide(conn, ["steps", "mood"], "2026-08-01", "2026-08-01")
    assert rows == [{"date": "2026-08-01", "steps": 8000, "mood": 4}]


def test_bulk_import_measurements_default_rerun_is_idempotent_not_additive():
    # Re-running an unchanged import shouldn't double a "sum" metric.
    conn = _conn_with_schema()
    rows = [{"timestamp": "2026-08-01T12:00:00", "metric": "steps", "value": 8000}]
    bulk_import_measurements(conn, "csv", rows)
    bulk_import_measurements(conn, "csv", rows)
    assert daily_metrics_wide(conn, ["steps"], "2026-08-01", "2026-08-01") == [{"date": "2026-08-01", "steps": 8000}]


def test_bulk_import_measurements_replace_drops_dates_missing_from_the_new_run():
    conn = _conn_with_schema()
    bulk_import_measurements(
        conn,
        "csv",
        [
            {"timestamp": "2026-08-01T12:00:00", "metric": "steps", "value": 8000},
            {"timestamp": "2026-08-02T12:00:00", "metric": "steps", "value": 9000},
        ],
    )
    # Second run's source file no longer has 2026-08-02.
    bulk_import_measurements(
        conn, "csv", [{"timestamp": "2026-08-01T12:00:00", "metric": "steps", "value": 8500}], replace=True
    )
    rows = {r["date"]: r["steps"] for r in daily_metrics_wide(conn, ["steps"])}
    assert rows == {"2026-08-01": 8500}


def test_bulk_import_measurements_only_touches_rows_from_the_same_importer():
    conn = _conn_with_schema()
    upsert_daily_metric_measurements(conn, "2026-08-01", {"mood": 4})  # source=DAILY_LOG_SOURCE, no importer
    bulk_import_measurements(
        conn, "csv", [{"timestamp": "2026-08-01T12:00:00", "metric": "steps", "value": 8000}], replace=True
    )
    rows = daily_metrics_wide(conn, ["steps", "mood"], "2026-08-01", "2026-08-01")
    assert rows == [{"date": "2026-08-01", "steps": 8000, "mood": 4}]


def test_bulk_import_measurements_rejects_a_metric_with_no_aggregation_rule():
    conn = _conn_with_schema()
    with pytest.raises(ValueError, match="no aggregation_rules entry"):
        bulk_import_measurements(
            conn, "csv", [{"timestamp": "2026-08-01T12:00:00", "metric": "not_a_real_metric", "value": 1}]
        )


def test_daily_log_source_is_distinguishable_in_provenance():
    conn = _conn_with_schema()
    upsert_daily_metric_measurements(conn, "2026-08-01", {"steps": 8000})
    provenance = get_metric_provenance(conn, "steps", "2026-08-01")
    assert provenance["sources"][0]["source"] == DAILY_LOG_SOURCE
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


def test_insert_measurement_returns_id_and_persists_row(tmp_path):
    conn = connect_writable(tmp_path / "test.db")
    try:
        ensure_schema(conn)
        row_id = insert_measurement(
            conn, "2026-06-01T08:00:00", "resting_heart_rate", 58, unit="bpm", source="Apple Watch",
            source_type="wearable",
        )
        conn.row_factory = row_class()
        row = dict(conn.execute("SELECT * FROM measurements WHERE id = ?", (row_id,)).fetchone())
        assert row["metric"] == "resting_heart_rate"
        assert row["value"] == 58
        assert row["source"] == "Apple Watch"
    finally:
        conn.close()


def test_query_measurements_filters_by_metric_and_date_range(tmp_path):
    conn = connect_writable(tmp_path / "test.db")
    try:
        ensure_schema(conn)
        insert_measurement(conn, "2026-06-01T08:00:00", "steps", 100, source="A")
        insert_measurement(conn, "2026-06-02T08:00:00", "steps", 200, source="B")
        insert_measurement(conn, "2026-06-02T08:00:00", "mood", 7, source="B")

        steps_only = query_measurements(conn, metric="steps")
        assert {r["value"] for r in steps_only} == {100, 200}

        ranged = query_measurements(conn, start="2026-06-02", end="2026-06-02T23:59:59")
        assert len(ranged) == 2

        by_source = query_measurements(conn, source="A")
        assert len(by_source) == 1 and by_source[0]["value"] == 100
    finally:
        conn.close()


def test_insert_workout_session_returns_id_and_persists_row(tmp_path):
    conn = connect_writable(tmp_path / "test.db")
    try:
        ensure_schema(conn)
        row_id = insert_workout_session(
            conn,
            date="2026-09-12",
            activity_type="running",
            duration_minutes=60,
            start_time="18:30",
            intensity="high",
            avg_heart_rate=150,
            max_heart_rate=172,
            source="Apple Watch",
            notes="Loop around the park",
        )
        conn.row_factory = row_class()
        row = dict(conn.execute("SELECT * FROM workout_sessions WHERE id = ?", (row_id,)).fetchone())
        assert row["activity_type"] == "running"
        assert row["duration_minutes"] == 60
        assert row["intensity"] == "high"
        assert row["avg_heart_rate"] == 150
        assert row["source"] == "Apple Watch"
    finally:
        conn.close()


def test_query_workout_sessions_filters_by_date_range_and_activity_type(tmp_path):
    conn = connect_writable(tmp_path / "test.db")
    try:
        ensure_schema(conn)
        insert_workout_session(conn, date="2026-09-10", activity_type="running", duration_minutes=30)
        insert_workout_session(conn, date="2026-09-12", activity_type="cycling", duration_minutes=45)
        insert_workout_session(conn, date="2026-09-12", activity_type="running", duration_minutes=20)

        by_date = query_workout_sessions(conn, start="2026-09-12", end="2026-09-12")
        assert len(by_date) == 2

        by_activity = query_workout_sessions(conn, activity_type="cycling")
        assert len(by_activity) == 1 and by_activity[0]["duration_minutes"] == 45
    finally:
        conn.close()


def test_workout_intensities_is_a_small_fixed_vocabulary():
    assert WORKOUT_INTENSITIES == {"low", "moderate", "high"}


def test_aggregate_measurements_to_daily_sums_and_averages_correctly(tmp_path):
    conn = connect_writable(tmp_path / "test.db")
    try:
        ensure_schema(conn)
        insert_measurement(conn, "2026-06-01T07:00:00", "steps", 3000)
        insert_measurement(conn, "2026-06-01T18:00:00", "steps", 4000)
        insert_measurement(conn, "2026-06-01T07:00:00", "resting_heart_rate", 60)
        insert_measurement(conn, "2026-06-01T20:00:00", "resting_heart_rate", 64)
        insert_measurement(conn, "2026-06-01T07:00:00", "weight_kg", 80.0)
        insert_measurement(conn, "2026-06-01T20:00:00", "weight_kg", 79.5)

        result = aggregate_measurements_to_daily(conn, "2026-06-01")
        assert result["date"] == "2026-06-01"
        assert result["steps"] == 7000
        assert result["resting_heart_rate"] == 62.0
        assert result["weight_kg"] == 79.5  # "last" picks the latest timestamp, not max value
    finally:
        conn.close()


def test_aggregate_measurements_to_daily_omits_metrics_with_no_rows(tmp_path):
    conn = connect_writable(tmp_path / "test.db")
    try:
        ensure_schema(conn)
        insert_measurement(conn, "2026-06-01T07:00:00", "steps", 1000)
        result = aggregate_measurements_to_daily(conn, "2026-06-01")
        assert set(result) == {"date", "steps"}
    finally:
        conn.close()


def test_measurements_migration_is_included_and_versioned():
    versions = [v for v, _desc, _fn in MIGRATIONS]
    assert 3 in versions
    assert SCHEMA_VERSION >= 3


def test_provenance_migration_adds_importer_and_imported_at():
    versions = [v for v, _desc, _fn in MIGRATIONS]
    assert 4 in versions
    assert SCHEMA_VERSION >= 4


def test_insert_measurement_stores_provenance_fields(tmp_path):
    conn = connect_writable(tmp_path / "test.db")
    try:
        ensure_schema(conn)
        row_id = insert_measurement(
            conn, "2026-06-01T08:00:00", "steps", 1000, source="Apple Watch",
            importer="apple-health", imported_at="2026-06-02T09:00:00",
        )
        conn.row_factory = row_class()
        row = dict(conn.execute("SELECT * FROM measurements WHERE id = ?", (row_id,)).fetchone())
        assert row["importer"] == "apple-health"
        assert row["imported_at"] == "2026-06-02T09:00:00"
    finally:
        conn.close()


def test_get_metric_provenance_reports_no_conflict_for_single_source(tmp_path):
    conn = connect_writable(tmp_path / "test.db")
    try:
        ensure_schema(conn)
        insert_measurement(conn, "2026-06-01T08:00:00", "resting_heart_rate", 60, source="Apple Watch")
        insert_measurement(conn, "2026-06-01T20:00:00", "resting_heart_rate", 62, source="Apple Watch")
        result = get_metric_provenance(conn, "resting_heart_rate", "2026-06-01")
        assert result["conflict"] is False
        assert len(result["sources"]) == 1
        assert result["sources"][0]["source"] == "Apple Watch"
        assert result["sources"][0]["n"] == 2
    finally:
        conn.close()


def test_get_metric_provenance_flags_conflict_across_sources(tmp_path):
    conn = connect_writable(tmp_path / "test.db")
    try:
        ensure_schema(conn)
        insert_measurement(conn, "2026-06-01T08:00:00", "resting_heart_rate", 62, source="Apple Watch")
        insert_measurement(conn, "2026-06-01T08:05:00", "resting_heart_rate", 67, source="Garmin")
        result = get_metric_provenance(conn, "resting_heart_rate", "2026-06-01")
        assert result["conflict"] is True
        assert {s["source"] for s in result["sources"]} == {"Apple Watch", "Garmin"}
    finally:
        conn.close()


def test_get_metric_provenance_ignores_close_readings_as_non_conflict(tmp_path):
    conn = connect_writable(tmp_path / "test.db")
    try:
        ensure_schema(conn)
        insert_measurement(conn, "2026-06-01T08:00:00", "resting_heart_rate", 60, source="Apple Watch")
        insert_measurement(conn, "2026-06-01T08:05:00", "resting_heart_rate", 61, source="Garmin")
        result = get_metric_provenance(conn, "resting_heart_rate", "2026-06-01")
        assert result["conflict"] is False
    finally:
        conn.close()


def test_resolve_source_conflicts_prefers_explicit_priority():
    rows = [
        {"source": "Garmin", "value": 67, "imported_at": "2026-06-02T09:00:00"},
        {"source": "Apple Watch", "value": 62, "imported_at": "2026-06-01T09:00:00"},
    ]
    kept, conflict = resolve_source_conflicts(rows, source_priority=["Apple Watch", "Garmin"])
    assert conflict is True
    assert {r["source"] for r in kept} == {"Apple Watch"}


def test_resolve_source_conflicts_falls_back_to_most_recently_imported():
    rows = [
        {"source": "Garmin", "value": 67, "imported_at": "2026-06-02T09:00:00"},
        {"source": "Apple Watch", "value": 62, "imported_at": "2026-06-01T09:00:00"},
    ]
    kept, conflict = resolve_source_conflicts(rows)
    assert conflict is True
    assert {r["source"] for r in kept} == {"Garmin"}


def test_resolve_source_conflicts_is_a_noop_for_a_single_source():
    rows = [{"source": "Apple Watch", "value": 62, "imported_at": None}]
    kept, conflict = resolve_source_conflicts(rows)
    assert kept == rows
    assert conflict is False


def test_aggregate_measurements_to_daily_uses_source_priority_to_resolve_conflict(tmp_path):
    conn = connect_writable(tmp_path / "test.db")
    try:
        ensure_schema(conn)
        insert_measurement(conn, "2026-06-01T08:00:00", "resting_heart_rate", 62, source="Apple Watch")
        insert_measurement(conn, "2026-06-01T08:05:00", "resting_heart_rate", 67, source="Garmin")
        result = aggregate_measurements_to_daily(conn, "2026-06-01", source_priority=["Apple Watch"])
        assert result["resting_heart_rate"] == 62.0
    finally:
        conn.close()


def test_connect_writable_sets_busy_timeout(tmp_path):
    conn = connect_writable(tmp_path / "test.db")
    try:
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout == BUSY_TIMEOUT_MS
    finally:
        conn.close()


def test_ensure_schema_enables_wal_mode(tmp_path):
    # A real file is needed here — :memory: databases can't use WAL, and
    # ensure_schema silently no-ops there rather than erroring (see the
    # in-memory tests above, which rely on exactly that behavior).
    conn = connect_writable(tmp_path / "test.db")
    try:
        ensure_schema(conn)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        conn.close()


def test_readonly_connection_reads_data_written_by_connect_writable(tmp_path):
    db_path = tmp_path / "test.db"
    conn = connect_writable(db_path)
    try:
        ensure_schema(conn)
        upsert_daily_metric_measurements(conn, "2026-01-01", {"steps": 5000})
    finally:
        conn.close()

    with readonly_connection(db_path) as conn:
        rows = daily_metrics_wide(conn, ["steps"], "2026-01-01", "2026-01-01")
        assert rows == [{"date": "2026-01-01", "steps": 5000}]


def test_readonly_connection_actually_blocks_writes(tmp_path):
    db_path = tmp_path / "test.db"
    conn = connect_writable(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    with readonly_connection(db_path) as conn, pytest.raises(sqlite3.Error):
        conn.execute("INSERT INTO measurements (timestamp, metric, value) VALUES ('2026-01-01T00:00:00', 'steps', 1)")


# ---------------------------------------------------------------------------
# Database encryption (#24)
# ---------------------------------------------------------------------------
#
# These exercise the real sqlcipher3 package (a dev-only dependency — see
# requirements-dev.txt) rather than mocking it, so a real incompatibility
# between sqlite3 and sqlcipher3 (like the Row-class one connect_writable
# and readonly_connection both have to work around) would actually be
# caught here. Skipped rather than failed if that optional package isn't
# importable, since it's not required to use this project without
# encryption.

require_sqlcipher = pytest.mark.skipif(not encryption_available(), reason="sqlcipher3 not installed")


def test_encryption_available_matches_whether_sqlcipher3_imports():
    import importlib.util

    assert encryption_available() == (importlib.util.find_spec("sqlcipher3") is not None)


def test_connect_writable_is_plain_sqlite_without_a_passphrase(tmp_path, monkeypatch):
    monkeypatch.delenv(DB_PASSPHRASE_ENV, raising=False)
    conn = connect_writable(tmp_path / "test.db")
    try:
        assert isinstance(conn, sqlite3.Connection)
        assert row_class() is sqlite3.Row
        assert db_error_types() == (sqlite3.Error,)
    finally:
        conn.close()


def test_setting_a_passphrase_without_sqlcipher3_installed_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.setenv(DB_PASSPHRASE_ENV, "secret")
    monkeypatch.setattr("logic._sqlcipher", None)  # simulate the package not being installed
    with pytest.raises(RuntimeError, match="sqlcipher3"):
        connect_writable(tmp_path / "test.db")


@require_sqlcipher
def test_connect_writable_encrypts_when_a_passphrase_is_set(tmp_path, monkeypatch):
    db_path = tmp_path / "encrypted.db"
    monkeypatch.setenv(DB_PASSPHRASE_ENV, "correct horse battery staple")
    conn = connect_writable(db_path)
    try:
        assert not isinstance(conn, sqlite3.Connection)  # a sqlcipher3 connection instead
        ensure_schema(conn)
        upsert_daily_metric_measurements(conn, "2026-01-01", {"steps": 4200})
    finally:
        conn.close()

    # The whole point: a plain sqlite3 connection, with no key, can't read
    # this file as a valid database — proving it's genuinely encrypted,
    # not just nominally opened through a different driver.
    monkeypatch.delenv(DB_PASSPHRASE_ENV, raising=False)
    plain_conn = sqlite3.connect(str(db_path))
    with pytest.raises(sqlite3.DatabaseError):
        plain_conn.execute("SELECT * FROM daily_metrics").fetchall()
    plain_conn.close()


@require_sqlcipher
def test_connect_writable_round_trips_through_readonly_connection_when_encrypted(tmp_path, monkeypatch):
    db_path = tmp_path / "encrypted.db"
    monkeypatch.setenv(DB_PASSPHRASE_ENV, "correct horse battery staple")
    conn = connect_writable(db_path)
    try:
        ensure_schema(conn)
        upsert_daily_metric_measurements(conn, "2026-01-01", {"steps": 4200})
    finally:
        conn.close()

    with readonly_connection(db_path) as conn:
        rows = daily_metrics_wide(conn, ["steps"], "2026-01-01", "2026-01-01")
        assert rows == [{"date": "2026-01-01", "steps": 4200}]


@require_sqlcipher
def test_wrong_passphrase_fails_loudly_rather_than_returning_garbage(tmp_path, monkeypatch):
    db_path = tmp_path / "encrypted.db"
    monkeypatch.setenv(DB_PASSPHRASE_ENV, "correct horse battery staple")
    conn = connect_writable(db_path)
    try:
        # An empty file has no encrypted header to validate a key
        # against, so SQLite treats it as valid regardless of passphrase
        # — there has to be real content on disk for a wrong key to
        # actually fail to decrypt it.
        ensure_schema(conn)
        upsert_daily_metric_measurements(conn, "2026-01-01", {"steps": 1})
    finally:
        conn.close()

    monkeypatch.setenv(DB_PASSPHRASE_ENV, "wrong passphrase entirely")
    with pytest.raises(RuntimeError, match="wrong"):
        connect_writable(db_path)


@require_sqlcipher
def test_db_error_types_includes_sqlcipher_errors_when_encrypted(tmp_path, monkeypatch):
    monkeypatch.setenv(DB_PASSPHRASE_ENV, "correct horse battery staple")
    error_types = db_error_types()
    assert sqlite3.Error in error_types
    assert len(error_types) == 2  # also includes sqlcipher3's own Error class


def test_default_data_dir_prefers_writable_source_checkout(tmp_path):
    # base_dir/data doesn't exist yet, but tmp_path is writable, so it
    # should be created and used rather than falling back.
    result = default_data_dir(tmp_path)
    assert result == tmp_path / "data"
    assert result.is_dir()


def test_default_data_dir_falls_back_when_source_checkout_is_not_writable(tmp_path, monkeypatch):
    import logic

    # Simulate a system-wide pip install: base_dir/data can't be written
    # to (e.g. it's inside a read-only site-packages), so this should
    # fall back to a per-user directory instead of raising or silently
    # picking an unusable path.
    monkeypatch.setattr(logic, "_dir_is_writable", lambda path: False)
    monkeypatch.setattr(logic, "_user_data_dir", lambda: tmp_path / "fake-home-data")

    result = default_data_dir(tmp_path)
    assert result == tmp_path / "fake-home-data" / "quantified-self-mcp"


def test_dir_is_writable_true_for_a_real_writable_path(tmp_path):
    from logic import _dir_is_writable

    assert _dir_is_writable(tmp_path / "new-subdir") is True


def test_dir_is_writable_false_when_path_is_actually_a_file(tmp_path):
    from logic import _dir_is_writable

    blocker = tmp_path / "not-a-directory"
    blocker.write_text("this occupies the path")
    assert _dir_is_writable(blocker) is False


def test_busy_timeout_lets_a_blocked_writer_wait_for_a_concurrent_write(tmp_path):
    """Exercises the exact contention scenario connect_writable/busy_timeout
    are meant to handle: one writer (e.g. init_db.py --replace) holds the
    write lock while a second (e.g. a concurrent log_daily_metric call)
    tries to write at the same time. Without a real busy_timeout, the
    second writer would fail immediately with 'database is locked'
    instead of waiting — the existing pragma-value test doesn't actually
    exercise contention, so it wouldn't catch a regression here.
    """
    db_path = tmp_path / "concurrent.db"
    setup_conn = connect_writable(db_path)
    ensure_schema(setup_conn)
    setup_conn.close()

    hold_seconds = 0.5
    lock_acquired = threading.Event()
    holder_errors = []

    def hold_write_lock():
        conn = connect_writable(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO measurements (timestamp, metric, value) VALUES (?, ?, ?)",
                ("2026-01-01T00:00:00", "steps", 1),
            )
            lock_acquired.set()
            time.sleep(hold_seconds)
            conn.commit()
        except Exception as exc:  # pragma: no cover - surfaced via holder_errors
            holder_errors.append(exc)
        finally:
            conn.close()

    holder = threading.Thread(target=hold_write_lock)
    holder.start()
    assert lock_acquired.wait(timeout=2), "holder thread never acquired the write lock"

    start = time.monotonic()
    second_conn = connect_writable(db_path)
    try:
        upsert_daily_metric_measurements(second_conn, "2026-01-02", {"steps": 2})
    finally:
        second_conn.close()
    elapsed = time.monotonic() - start

    holder.join(timeout=5)
    assert not holder_errors, f"lock-holding thread raised: {holder_errors}"

    # Genuinely waited for the first writer (proving busy_timeout works) —
    # neither failed instantly nor slipped in before the lock was held.
    assert elapsed >= hold_seconds * 0.6, (
        f"second write returned in {elapsed:.2f}s — too fast to have "
        "actually waited on the concurrent writer's lock"
    )
    assert elapsed < BUSY_TIMEOUT_MS / 1000, "second write took suspiciously close to the busy_timeout ceiling"

    check_conn = sqlite3.connect(str(db_path))
    rows = {r["date"]: r["steps"] for r in daily_metrics_wide(check_conn, ["steps"])}
    check_conn.close()
    assert rows == {"2026-01-01": 1, "2026-01-02": 2}


def test_concurrent_upserts_from_multiple_threads_all_succeed(tmp_path):
    """Simulates several near-simultaneous log_daily_metric-style calls
    (e.g. Claude Desktop firing off a few tool calls in quick succession)
    hitting the same database file. WAL mode plus busy_timeout should let
    every write eventually land rather than a subset failing with
    'database is locked'.
    """
    db_path = tmp_path / "concurrent_multi.db"
    setup_conn = connect_writable(db_path)
    ensure_schema(setup_conn)
    setup_conn.close()

    thread_count = 10
    errors = []

    def write_one(i):
        conn = connect_writable(db_path)
        try:
            upsert_daily_metric_measurements(conn, f"2026-02-{i + 1:02d}", {"steps": i * 100})
        except Exception as exc:  # pragma: no cover - surfaced via errors
            errors.append((i, exc))
        finally:
            conn.close()

    threads = [threading.Thread(target=write_one, args=(i,)) for i in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"some concurrent writes failed: {errors}"

    check_conn = sqlite3.connect(str(db_path))
    count = check_conn.execute("SELECT COUNT(*) FROM daily_metrics").fetchone()[0]
    check_conn.close()
    assert count == thread_count
