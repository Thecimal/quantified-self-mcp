"""
Source resolution in the daily_metrics projection.

daily_metrics used to aggregate *every* measurement for a (metric, day), so
two devices recording the same walk were summed (19,500 steps from 10,000 +
9,500) and readings from different devices were averaged into a number
neither reported. The projection now aggregates only one source's
observations per (metric, day), chosen by db/aggregation.py:

  1. the highest-ranked present source in source_priority (a metric's own
     list if it has one, otherwise the '*' list),
  2. otherwise the source that observed the most distinct hours of the day,
  3. then the one with the latest observation,
  4. then source name (a missing source sorts last).

These tests cover that order, the trigger/repair/verify parity, the v8
migration, and the Python preview (logic.resolve_source_conflicts) staying
in step with the SQL.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import invariant
from logic import (
    DAILY_LOG_SOURCE,
    SCHEMA_VERSION,
    aggregate_measurements_to_daily,
    bulk_import_measurements,
    ensure_schema,
    insert_measurement,
    resolve_source_conflicts,
    upsert_daily_metric_measurements,
)

DAY = "2026-03-01"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    ensure_schema(c)
    return c


def _row(conn, metric, day=DAY):
    found = conn.execute(
        "SELECT value, raw_measurement_count, resolved_source, source_count, resolution "
        "FROM daily_metrics WHERE metric = ? AND date = ?",
        (metric, day),
    ).fetchone()
    if found is None:
        return None
    return dict(zip(("value", "count", "source", "sources", "resolution"), found, strict=True))


def _add(conn, hour_minute, metric, value, source=None, day=DAY):
    insert_measurement(conn, f"{day}T{hour_minute}:00", metric, value, source=source)


# --- the motivating cases ---------------------------------------------------


def test_two_devices_are_resolved_to_one_not_summed(conn):
    _add(conn, "06:00", "steps", 3000, "Apple Watch")
    _add(conn, "12:00", "steps", 3500, "Apple Watch")
    _add(conn, "18:00", "steps", 3500, "Apple Watch")
    _add(conn, "23:59", "steps", 9500, "Garmin")
    assert _row(conn, "steps") == {
        "value": 10000.0,
        "count": 3,
        "source": "Apple Watch",
        "sources": 2,
        "resolution": "fallback",
    }


def test_sleep_from_two_devices_is_not_added_together(conn):
    _add(conn, "07:00", "sleep_hours", 7.4, "Apple Health")
    _add(conn, "07:30", "sleep_hours", 7.1, "Oura")
    got = _row(conn, "sleep_hours")
    assert got["value"] in (7.4, 7.1)
    assert got["sources"] == 2


def test_mean_metric_is_not_blended_across_devices(conn):
    _add(conn, "08:00", "heart_rate", 60, "Apple Watch")
    _add(conn, "09:00", "heart_rate", 62, "Apple Watch")
    _add(conn, "08:30", "heart_rate", 68, "Garmin")
    got = _row(conn, "heart_rate")
    assert got["value"] == 61.0
    assert got["source"] == "Apple Watch"


def test_apple_import_manual_log_and_csv_total_no_longer_stack(conn):
    bulk_import_measurements(
        conn,
        "apple-health",
        [
            {"timestamp": f"{DAY}T10:00:00", "metric": "steps", "value": 6000, "source": "iPhone"},
            {"timestamp": f"{DAY}T10:00:00", "metric": "steps", "value": 5900, "source": "Apple Watch"},
        ],
    )
    upsert_daily_metric_measurements(conn, DAY, {"steps": 12000})
    bulk_import_measurements(conn, "csv", [{"timestamp": f"{DAY}T12:00:00", "metric": "steps", "value": 12000}])
    got = _row(conn, "steps")
    assert got["value"] == 12000.0
    assert got["source"] == DAILY_LOG_SOURCE
    assert got["sources"] == 4
    assert got["resolution"] == "priority"


def test_single_source_days_are_unchanged(conn):
    _add(conn, "07:00", "steps", 3000, "Apple Watch")
    _add(conn, "18:00", "steps", 4000, "Apple Watch")
    assert _row(conn, "steps") == {
        "value": 7000.0,
        "count": 2,
        "source": "Apple Watch",
        "sources": 1,
        "resolution": "single",
    }


# --- fallback order: coverage, then latest observation, then name -----------


def test_coverage_counts_distinct_hours_not_observations(conn):
    for minute in range(50):  # 50 readings, all inside one hour
        _add(conn, f"08:{minute:02d}", "heart_rate", 70, "Chatty Device")
    _add(conn, "07:00", "heart_rate", 60, "Quiet Device")
    _add(conn, "12:00", "heart_rate", 60, "Quiet Device")
    _add(conn, "20:00", "heart_rate", 60, "Quiet Device")
    assert _row(conn, "heart_rate")["source"] == "Quiet Device"


def test_latest_observation_breaks_a_coverage_tie(conn):
    _add(conn, "08:00", "heart_rate", 60, "Apple Watch")
    _add(conn, "08:30", "heart_rate", 70, "Garmin")
    assert _row(conn, "heart_rate")["source"] == "Garmin"


def test_source_name_breaks_a_full_tie(conn):
    _add(conn, "08:00", "heart_rate", 70, "Garmin")
    _add(conn, "08:00", "heart_rate", 60, "Apple Watch")
    assert _row(conn, "heart_rate")["source"] == "Apple Watch"


def test_a_missing_source_loses_a_full_tie_to_a_named_one(conn):
    _add(conn, "08:00", "heart_rate", 70, None)
    _add(conn, "08:00", "heart_rate", 60, "Garmin")
    assert _row(conn, "heart_rate")["source"] == "Garmin"


def test_last_metric_uses_the_resolved_sources_latest_reading(conn):
    _add(conn, "08:00", "weight_kg", 80.0, "Scale A")
    _add(conn, "20:00", "weight_kg", 79.0, "Scale B")
    assert _row(conn, "weight_kg")["value"] == 79.0


# --- explicit priority -------------------------------------------------------


def test_explicit_priority_beats_coverage(conn):
    invariant.set_source_priority(conn, "*", ["Garmin"])
    _add(conn, "06:00", "steps", 3000, "Apple Watch")
    _add(conn, "12:00", "steps", 3500, "Apple Watch")
    _add(conn, "23:59", "steps", 9500, "Garmin")
    got = _row(conn, "steps")
    assert (got["value"], got["source"], got["resolution"]) == (9500.0, "Garmin", "priority")


def test_priority_takes_the_first_ranked_source_that_is_present(conn):
    invariant.set_source_priority(conn, "*", ["A", "B", "C"])
    _add(conn, "08:00", "heart_rate", 60, "C")
    _add(conn, "08:00", "heart_rate", 70, "B")
    assert _row(conn, "heart_rate")["source"] == "B"


def test_a_metrics_own_list_overrides_the_global_list(conn):
    invariant.set_source_priority(conn, "*", ["Garmin"])
    invariant.set_source_priority(conn, "steps", ["Apple Watch"])
    for metric in ("steps", "heart_rate"):
        _add(conn, "08:00", metric, 60, "Apple Watch")
        _add(conn, "08:00", metric, 70, "Garmin")
    assert _row(conn, "steps")["source"] == "Apple Watch"
    assert _row(conn, "heart_rate")["source"] == "Garmin"


def test_default_priority_puts_manual_daily_log_first(conn):
    assert invariant.get_source_priority(conn) == {"*": [DAILY_LOG_SOURCE]}
    _add(conn, "06:00", "steps", 5000, "Apple Watch")
    _add(conn, "12:00", "steps", 5000, "Apple Watch")
    upsert_daily_metric_measurements(conn, DAY, {"steps": 8000})
    got = _row(conn, "steps")
    assert (got["value"], got["source"], got["resolution"]) == (8000.0, DAILY_LOG_SOURCE, "priority")


def test_removed_default_priority_is_not_reseeded_on_next_start(conn):
    invariant.set_source_priority(conn, "*", [])
    assert invariant.get_source_priority(conn) == {}
    ensure_schema(conn)
    assert invariant.get_source_priority(conn) == {}


# --- the projection stays correct as measurements change ---------------------


def test_deleting_the_winning_sources_rows_re_resolves_the_day(conn):
    _add(conn, "06:00", "steps", 3000, "Apple Watch")
    _add(conn, "12:00", "steps", 3500, "Apple Watch")
    _add(conn, "23:59", "steps", 9500, "Garmin")
    assert _row(conn, "steps")["source"] == "Apple Watch"
    conn.execute("DELETE FROM measurements WHERE source = 'Apple Watch'")
    conn.commit()
    assert _row(conn, "steps") == {
        "value": 9500.0,
        "count": 1,
        "source": "Garmin",
        "sources": 1,
        "resolution": "single",
    }
    conn.execute("DELETE FROM measurements")
    conn.commit()
    assert _row(conn, "steps") is None


def test_moving_a_measurement_to_another_day_re_resolves_both_days(conn):
    _add(conn, "08:00", "steps", 4000, "Apple Watch")
    _add(conn, "08:00", "steps", 9000, "Garmin")
    _add(conn, "09:00", "steps", 500, "Garmin")
    assert _row(conn, "steps")["source"] == "Garmin"
    conn.execute("UPDATE measurements SET timestamp = '2026-03-02T09:00:00' WHERE value = 500")
    conn.commit()
    # 03-01 is now a full tie (both sources observed only hour 08, latest 08:00), so the name decides.
    assert _row(conn, "steps")["source"] == "Apple Watch"
    assert _row(conn, "steps", "2026-03-02") == {
        "value": 500.0,
        "count": 1,
        "source": "Garmin",
        "sources": 1,
        "resolution": "single",
    }


def test_changing_a_measurements_source_re_resolves_the_day(conn):
    _add(conn, "08:00", "heart_rate", 60, "Apple Watch")
    _add(conn, "09:00", "heart_rate", 62, "Apple Watch")
    _add(conn, "08:00", "heart_rate", 90, "Garmin")
    assert _row(conn, "heart_rate")["source"] == "Apple Watch"
    conn.execute("UPDATE measurements SET source = 'Garmin' WHERE source = 'Apple Watch'")
    conn.commit()
    assert _row(conn, "heart_rate")["sources"] == 1
    assert _row(conn, "heart_rate")["count"] == 3


# --- set_source_priority -----------------------------------------------------


def test_set_source_priority_re_projects_existing_days_and_verifies_clean(conn):
    _add(conn, "06:00", "steps", 3000, "Apple Watch")
    _add(conn, "12:00", "steps", 3500, "Apple Watch")
    _add(conn, "23:59", "steps", 9500, "Garmin")
    assert _row(conn, "steps")["source"] == "Apple Watch"
    result = invariant.set_source_priority(conn, "steps", ["Garmin", "Apple Watch"])
    assert result == {"status": "ok", "issues": []}
    assert _row(conn, "steps")["value"] == 9500.0
    assert invariant.get_source_priority(conn)["steps"] == ["Garmin", "Apple Watch"]
    invariant.set_source_priority(conn, "steps", [])
    assert "steps" not in invariant.get_source_priority(conn)
    assert _row(conn, "steps")["source"] == "Apple Watch"


def test_set_source_priority_rejects_bad_input_without_changing_anything(conn):
    before = invariant.get_source_priority(conn)
    with pytest.raises(ValueError, match="unknown metric"):
        invariant.set_source_priority(conn, "not_a_metric", ["A"])
    with pytest.raises(ValueError, match="duplicate"):
        invariant.set_source_priority(conn, "steps", ["A", "A"])
    with pytest.raises(ValueError, match="non-empty"):
        invariant.set_source_priority(conn, "steps", ["A", ""])
    assert invariant.get_source_priority(conn) == before


def test_editing_the_priority_table_directly_is_caught_by_verify_and_fixed_by_repair(conn):
    _add(conn, "06:00", "steps", 3000, "Apple Watch")
    _add(conn, "12:00", "steps", 3500, "Apple Watch")
    _add(conn, "23:59", "steps", 9500, "Garmin")
    conn.execute("INSERT INTO source_priority (metric, rank, source) VALUES ('steps', 1, 'Garmin')")
    conn.commit()
    report = invariant.verify(conn)
    assert report["status"] == "mismatch"
    assert [(i["metric"], i["issue"]) for i in report["issues"]] == [("steps", "missing_or_stale")]
    assert invariant.repair(conn) == {"status": "ok", "issues": []}
    assert _row(conn, "steps")["source"] == "Garmin"


def test_verify_flags_stale_resolution_metadata_even_when_the_value_is_right(conn):
    _add(conn, "08:00", "steps", 5000, "Apple Watch")
    _add(conn, "08:00", "steps", 5000, "Garmin")
    assert _row(conn, "steps")["source"] == "Apple Watch"
    for column, stale in (("resolved_source", "'Garmin'"), ("source_count", "1"), ("resolution", "'priority'")):
        invariant.repair(conn)
        conn.execute(f"UPDATE daily_metrics SET {column} = {stale} WHERE metric = 'steps'")
        conn.commit()
        report = invariant.verify(conn)
        assert report["status"] == "mismatch", column
        assert [i["metric"] for i in report["issues"]] == ["steps"], column
    assert invariant.repair(conn) == {"status": "ok", "issues": []}


# --- trigger path, repair path and Python preview must agree -----------------


def _parity_rows():
    rows = []

    def add(day, hm, metric, value, source=None):
        rows.append({"timestamp": f"{day}T{hm}:00", "metric": metric, "value": value, "source": source})

    for hm, v in (("06:00", 3000), ("12:00", 3500), ("18:00", 3500)):
        add("2026-04-01", hm, "steps", v, "Apple Watch")
    add("2026-04-01", "23:59", "steps", 9500, "Garmin")
    add("2026-04-01", "12:00", "steps", 12000, None)
    add("2026-04-02", "08:00", "heart_rate", 61, "Apple Watch")
    add("2026-04-02", "09:00", "heart_rate", 63, "Apple Watch")
    add("2026-04-02", "08:30", "heart_rate", 68, "Garmin")
    add("2026-04-03", "08:00", "weight_kg", 80.0, "Scale A")
    add("2026-04-03", "20:00", "weight_kg", 79.0, "Scale B")
    add("2026-04-04", "08:00", "sleep_hours", 7.4, "Apple Health")
    add("2026-04-04", "08:00", "sleep_hours", 7.1, "Oura")
    add("2026-04-05", "10:00", "water_ml", 500, "Water App")
    add("2026-04-05", "15:00", "water_ml", 700, "Water App")
    add("2026-04-05", "12:00", "water_ml", 1200, DAILY_LOG_SOURCE)
    add("2026-04-06", "07:00", "mood", 6, None)
    return rows


_PROJECTION = (
    "SELECT date, metric, value, raw_measurement_count, aggregation_method, resolved_source, source_count, resolution "
    "FROM daily_metrics ORDER BY date, metric"
)


def test_bulk_import_path_projects_exactly_what_the_live_triggers_do():
    live = sqlite3.connect(":memory:")
    ensure_schema(live)
    for row in _parity_rows():
        insert_measurement(live, row["timestamp"], row["metric"], row["value"], source=row["source"])

    bulk = sqlite3.connect(":memory:")
    ensure_schema(bulk)
    assert invariant.bulk_insert_measurements(bulk, _parity_rows()) == {"status": "ok", "issues": []}

    assert bulk.execute(_PROJECTION).fetchall() == live.execute(_PROJECTION).fetchall()
    assert invariant.verify(live) == {"status": "ok", "issues": []}


def test_python_preview_agrees_with_the_stored_projection():
    c = sqlite3.connect(":memory:")
    ensure_schema(c)
    invariant.bulk_insert_measurements(c, _parity_rows())
    invariant.set_source_priority(c, "water_ml", ["Water App"])
    days = [r[0] for r in c.execute("SELECT DISTINCT date FROM daily_metrics")]
    assert len(days) == 6
    for day in days:
        preview = aggregate_measurements_to_daily(c, day)
        stored = {m: v for m, v in c.execute("SELECT metric, value FROM daily_metrics WHERE date = ?", (day,))}
        assert set(preview) - {"date"} == set(stored), day
        for metric, value in stored.items():
            assert preview[metric] == pytest.approx(value, abs=0.06), (day, metric)


def test_preview_with_an_explicit_priority_overrides_the_stored_one(conn):
    _add(conn, "08:00", "heart_rate", 62, "Apple Watch")
    _add(conn, "09:00", "heart_rate", 64, "Apple Watch")
    _add(conn, "08:00", "heart_rate", 67, "Garmin")
    assert aggregate_measurements_to_daily(conn, DAY)["heart_rate"] == 63.0
    assert aggregate_measurements_to_daily(conn, DAY, source_priority=["Garmin"])["heart_rate"] == 67.0
    assert _row(conn, "heart_rate")["value"] == 63.0


def test_resolve_source_conflicts_falls_back_to_hours_then_latest_then_name():
    def rows(*specs):
        return [{"source": s, "timestamp": f"{DAY}T{hm}:00", "value": 1} for s, hm in specs]

    def winner(*specs):
        kept, conflict = resolve_source_conflicts(rows(*specs))
        assert conflict is True
        return {r["source"] for r in kept}

    assert winner(("A", "08:00"), ("B", "08:30"), ("B", "09:00")) == {"B"}  # more hours
    assert winner(("A", "08:00"), ("B", "08:30")) == {"B"}  # latest observation
    assert winner(("B", "08:00"), ("A", "08:00")) == {"A"}  # name
    assert winner((None, "08:00"), ("A", "08:00")) == {"A"}  # a missing source sorts last


# --- schema pieces -----------------------------------------------------------


def test_v8_migration_upgrades_a_v7_database_and_re_resolves_blended_days():
    old = sqlite3.connect(":memory:")
    ensure_schema(old)
    invariant.drop_triggers(old)
    old.executescript(
        """
        DROP TABLE daily_metrics;
        DROP TABLE source_priority;
        CREATE TABLE daily_metrics (
            date TEXT NOT NULL, metric TEXT NOT NULL, value REAL NOT NULL,
            raw_measurement_count INTEGER NOT NULL, aggregation_method TEXT NOT NULL,
            aggregated_at TEXT NOT NULL, PRIMARY KEY (date, metric)
        );
        INSERT INTO measurements (timestamp, metric, value, source) VALUES
            ('2026-03-01T08:00:00', 'steps', 10000, 'Apple Watch'),
            ('2026-03-01T09:00:00', 'steps', 9500, 'Garmin');
        INSERT INTO daily_metrics VALUES ('2026-03-01', 'steps', 19500, 2, 'sum', '2026-03-01 00:00:00');
        PRAGMA user_version = 7;
        """
    )
    old.commit()

    ensure_schema(old)

    assert old.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION >= 8
    columns = {r[1] for r in old.execute("PRAGMA table_info(daily_metrics)")}
    assert {"resolved_source", "source_count", "resolution"} <= columns
    assert invariant.get_source_priority(old) == {"*": [DAILY_LOG_SOURCE]}
    got = old.execute("SELECT value, source_count, resolution FROM daily_metrics WHERE metric = 'steps'").fetchone()
    assert got[1:] == (2, "fallback")
    assert got[0] in (10000.0, 9500.0)
    assert invariant.verify(old) == {"status": "ok", "issues": []}


def test_measurements_has_the_expression_index_the_projection_queries_rely_on(conn):
    indexes = {r[1] for r in conn.execute("PRAGMA index_list(measurements)")}
    assert "idx_measurements_metric_day_source" in indexes
