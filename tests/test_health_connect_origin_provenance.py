"""
Health Connect import: records from different data origins are not added together.

Health Connect lets several apps write the same kind of record, so one walk can arrive twice, once from a fitness app
and once from the phone vendor's health app. The importer used to add every steps, workout and sleep record of a day
into one total stored under the importer's name, ignoring metadata.dataOrigin: that walk counted twice and neither
app's name survived. These tests drive the real import path (file -> adapter -> measurements -> daily_metrics) and
check what is stored and what the daily view shows. Nothing is merged because a value or a time matches: both origins'
rows are kept, and the daily view picks one source, as it already does for Apple Health.
"""

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import invariant  # noqa: E402
from init_db import init_health_db  # noqa: E402
from logic import connect_writable, ensure_schema, import_measurements  # noqa: E402

WALKER = "com.example.walker"
TRACKER = "com.example.tracker"
DAY = "2026-06-01"


def _steps(hour, count, origin=None, record_id=None, day=DAY):
    record = {
        "recordType": "StepsRecord",
        "startTime": f"{day}T{hour:02d}:00:00Z",
        "endTime": f"{day}T{hour:02d}:10:00Z",
        "count": count,
    }
    metadata = {}
    if record_id is not None:
        metadata["id"] = record_id
    if origin is not None:
        metadata["dataOrigin"] = {"packageName": origin}
    if metadata:
        record["metadata"] = metadata
    return record


def _import(tmp_path, records):
    path = tmp_path / "health_connect.json"
    path.write_text(json.dumps(records), encoding="utf-8")
    init_health_db(path, tmp_path / "health.db", replace=False, source="health-connect")
    return tmp_path / "health.db"


def _rows(db, metric="steps", day=DAY):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT source, value, importer, timestamp FROM measurements "
            "WHERE metric = ? AND substr(timestamp, 1, 10) = ? ORDER BY source",
            (metric, day),
        ).fetchall()
    finally:
        conn.close()


def _day(db, metric="steps", day=DAY):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT value, resolved_source, source_count, resolution FROM daily_metrics WHERE metric = ? AND date = ?",
            (metric, day),
        ).fetchone()
    finally:
        conn.close()


def test_the_same_walk_from_two_origins_is_kept_under_both_and_counted_once_in_the_day(tmp_path, capsys):
    db = _import(tmp_path, [_steps(8, 2000, WALKER, "w1"), _steps(8, 2000, TRACKER, "t1")])
    capsys.readouterr()

    # Both origins survive, each with its own total, under its own package name and the importer that read it.
    assert [(r[0], r[1], r[2]) for r in _rows(db)] == [
        (TRACKER, 2000.0, "health-connect"),
        (WALKER, 2000.0, "health-connect"),
    ]
    value, source, source_count, resolution = _day(db)
    assert value == 2000  # one origin's walk, not 4000
    assert source in {WALKER, TRACKER}
    assert (source_count, resolution) == (2, "fallback")
    conn = sqlite3.connect(db)
    try:
        assert invariant.verify(conn)["status"] == "ok"
    finally:
        conn.close()


def test_origins_that_disagree_are_not_blended_and_the_day_shows_one_of_them(tmp_path, capsys):
    db = _import(tmp_path, [_steps(8, 2000, WALKER), _steps(8, 1500, TRACKER)])
    capsys.readouterr()

    assert {r[0]: r[1] for r in _rows(db)} == {WALKER: 2000.0, TRACKER: 1500.0}
    value, source, source_count, _ = _day(db)
    assert (value, source_count) == ({WALKER: 2000, TRACKER: 1500}[source], 2)  # one source's own total


def test_distinct_measurements_with_similar_values_are_not_merged_within_an_origin(tmp_path, capsys):
    # Same origin, same value, different times, and a value one step apart: all genuinely separate readings.
    db = _import(
        tmp_path,
        [_steps(8, 2000, WALKER, "w1"), _steps(9, 2000, WALKER, "w2"), _steps(10, 2001, WALKER, "w3")],
    )
    capsys.readouterr()

    assert [(r[0], r[1]) for r in _rows(db)] == [(WALKER, 6001.0)]
    assert _day(db) == (6001.0, WALKER, 1, "single")


def test_a_value_shared_across_origins_at_different_times_is_still_two_separate_sources(tmp_path, capsys):
    db = _import(tmp_path, [_steps(8, 2000, WALKER, "w1"), _steps(20, 2000, TRACKER, "t1")])
    capsys.readouterr()

    # Nothing is dropped or merged on matching values: both origins' rows are stored.
    assert {r[0]: r[1] for r in _rows(db)} == {WALKER: 2000.0, TRACKER: 2000.0}
    assert _day(db)[2] == 2


def test_reimporting_the_same_export_changes_nothing(tmp_path, capsys):
    records = [_steps(8, 2000, WALKER, "w1"), _steps(8, 2000, TRACKER, "t1")]
    db = _import(tmp_path, records)
    conn = sqlite3.connect(db)
    before = conn.execute("SELECT id, timestamp, metric, value, source, imported_at FROM measurements").fetchall()
    capsys.readouterr()

    _import(tmp_path, records)
    out = capsys.readouterr().out

    assert "0 added, 0 updated" in out
    assert (
        conn.execute("SELECT id, timestamp, metric, value, source, imported_at FROM measurements").fetchall() == before
    )
    conn.close()


def test_records_with_missing_or_unusable_origin_get_no_invented_origin_and_are_not_merged_into_one(tmp_path, capsys):
    blank = _steps(9, 300)
    blank["metadata"] = {"dataOrigin": {"packageName": "   "}}
    not_a_name = _steps(10, 400)
    not_a_name["metadata"] = {"dataOrigin": {"packageName": 7}}
    db = _import(tmp_path, [_steps(8, 2000, WALKER), _steps(8, 100), blank, not_a_name])
    capsys.readouterr()

    # The three records with no usable origin are stamped with the importer's name (the documented stamp for a
    # record with no source of its own) and add up as one unattributed group; WALKER's total stays its own.
    assert {r[0]: r[1] for r in _rows(db)} == {WALKER: 2000.0, "health-connect": 800.0}
    value, source, source_count, _ = _day(db)
    assert source_count == 2  # the unattributed group is not folded into a named origin
    assert (value, source) == (2000, WALKER)  # equal evidence, so the documented tie-break: source name


def test_a_record_repeated_with_the_same_id_is_still_counted_once_within_its_origin(tmp_path, capsys):
    db = _import(tmp_path, [_steps(8, 1000, WALKER, "a1"), _steps(8, 1000, WALKER, "a1"), _steps(9, 500, WALKER)])
    capsys.readouterr()

    assert [(r[0], r[1]) for r in _rows(db)] == [(WALKER, 1500.0)]  # 1000 once + 500, not 2500


def test_workout_minutes_and_sleep_from_two_origins_are_not_added_together_either(tmp_path, capsys):
    def exercise(origin):
        return {
            "recordType": "ExerciseSessionRecord",
            "startTime": f"{DAY}T07:00:00Z",
            "endTime": f"{DAY}T07:30:00Z",
            "metadata": {"dataOrigin": {"packageName": origin}},
        }

    def sleep(origin):
        return {
            "recordType": "SleepSessionRecord",
            "startTime": f"{DAY}T00:00:00Z",
            "endTime": f"{DAY}T08:00:00Z",
            "metadata": {"dataOrigin": {"packageName": origin}},
        }

    db = _import(tmp_path, [exercise(WALKER), exercise(TRACKER), sleep(WALKER), sleep(TRACKER)])
    capsys.readouterr()

    assert {r[0]: r[1] for r in _rows(db, "workout_minutes")} == {WALKER: 30.0, TRACKER: 30.0}
    assert {r[0]: r[1] for r in _rows(db, "sleep_hours")} == {WALKER: 8.0, TRACKER: 8.0}
    assert _day(db, "workout_minutes")[0] == 30  # not 60
    assert _day(db, "sleep_hours")[0] == 8.0  # not 16


def test_averaged_metrics_are_unchanged_and_still_stored_as_one_day_value(tmp_path, capsys):
    resting = {
        "recordType": "RestingHeartRateRecord",
        "time": f"{DAY}T06:00:00Z",
        "beatsPerMinute": 60,
        "metadata": {"dataOrigin": {"packageName": WALKER}},
    }
    db = _import(tmp_path, [resting, _steps(8, 1000, WALKER)])
    capsys.readouterr()

    assert [(r[0], r[1]) for r in _rows(db, "resting_heart_rate")] == [("health-connect", 60.0)]
    assert _day(db, "resting_heart_rate")[0] == 60


def test_a_database_imported_before_the_fix_is_corrected_in_place_by_a_reimport(tmp_path, capsys):
    db = tmp_path / "health.db"
    conn = connect_writable(db)
    ensure_schema(conn)
    # What the earlier adapter wrote for the day: both origins added up into one noon row under the importer.
    import_measurements(conn, "health-connect", [{"timestamp": f"{DAY}T12:00:00", "metric": "steps", "value": 4000.0}])
    conn.close()

    _import(tmp_path, [_steps(8, 2000, WALKER, "w1"), _steps(8, 2000, TRACKER, "t1")])
    capsys.readouterr()

    assert {r[0]: r[1] for r in _rows(db)} == {WALKER: 2000.0, TRACKER: 2000.0}
    assert _day(db)[0] == 2000
