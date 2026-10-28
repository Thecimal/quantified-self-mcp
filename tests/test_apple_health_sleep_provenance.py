"""
Source provenance through the Apple Health adapter, for sleep.

Steps, weight, heart rate and the other mapped Apple Health types reach `measurements` one row per record with
the record's own sourceName. Sleep did not: its intervals were summed per day across every source and stored as
one noon row under the importer, so a night recorded by two sources (a watch and a sleep app) was added up as if
it were one source's single night, and neither source survived. These tests drive the real import path on a small
synthetic export with two distinct sources and check what is stored, retrieved and projected.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import invariant  # noqa: E402
from init_db import init_health_db  # noqa: E402
from logic import connect_writable, ensure_schema, import_measurements, query_measurements  # noqa: E402

WATCH = "Synthetic Watch"
APP = "Synthetic Sleep App"
NIGHT = "2026-03-01"
UNNAMED_NIGHT = "2026-03-02"


def _asleep(start, end, source=None):
    named = f' sourceName="{source}"' if source is not None else ""
    return (
        f'<Record type="HKCategoryTypeIdentifierSleepAnalysis"{named} '
        'value="HKCategoryValueSleepAnalysisAsleepCore" '
        f'startDate="{start} +0000" endDate="{end} +0000"/>'
    )


def _steps(source, value, when):
    return (
        f'<Record type="HKQuantityTypeIdentifierStepCount" sourceName="{source}" unit="count" '
        f'startDate="{when} +0000" endDate="{when} +0000" value="{value}"/>'
    )


# The same night seen by two sources, 6 h each and overlapping (so 12 h would be a double count), a second night
# with one record that names no source at all, and steps from the same two sources.
RECORDS = [
    _asleep("2026-03-01 00:00:00", "2026-03-01 06:00:00", WATCH),
    _asleep("2026-03-01 00:10:00", "2026-03-01 06:10:00", APP),
    _asleep("2026-03-02 00:00:00", "2026-03-02 07:00:00"),
    _steps(WATCH, 4000, "2026-03-01 08:00:00"),
    _steps(APP, 3000, "2026-03-01 09:00:00"),
]


def _export(tmp_path):
    path = tmp_path / "export.xml"
    path.write_text('<?xml version="1.0"?><HealthData>' + "".join(RECORDS) + "</HealthData>", encoding="utf-8")
    return path


def _import(tmp_path):
    init_health_db(_export(tmp_path), tmp_path / "health.db", replace=False, source="apple-health")
    return tmp_path / "health.db"


def _sleep_rows(db, day):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT timestamp, value, source, importer, imported_at FROM measurements "
            "WHERE metric = 'sleep_hours' AND substr(timestamp, 1, 10) = ? ORDER BY source",
            (day,),
        ).fetchall()
    finally:
        conn.close()


def test_each_sleep_source_survives_import_and_can_be_retrieved_by_name(tmp_path, capsys):
    db = _import(tmp_path)
    capsys.readouterr()

    rows = _sleep_rows(db, NIGHT)
    assert [(r[1], r[2], r[3]) for r in rows] == [(6.0, APP, "apple-health"), (6.0, WATCH, "apple-health")]
    # A day total keeps the project's noon placeholder (only the day is claimed), the same for every source.
    assert {r[2]: r[0] for r in rows} == {WATCH: f"{NIGHT}T12:00:00", APP: f"{NIGHT}T12:00:00"}
    # One import ran once: both rows say when, and it is the same moment.
    assert rows[0][4] is not None and rows[0][4] == rows[1][4]

    conn = connect_writable(db)
    try:
        by_source = query_measurements(conn, metric="sleep_hours", source=APP)
        assert [(m["source"], m["value"], m["importer"]) for m in by_source] == [(APP, 6.0, "apple-health")]
        assert query_measurements(conn, metric="steps", source=WATCH)[0]["value"] == 4000.0
        assert query_measurements(conn, metric="steps", source=APP)[0]["value"] == 3000.0
    finally:
        conn.close()


def test_two_sources_are_not_added_together_and_the_day_says_two_sources_were_present(tmp_path, capsys):
    db = _import(tmp_path)
    capsys.readouterr()

    conn = sqlite3.connect(db)
    try:
        value, count, source, source_count, resolution = conn.execute(
            "SELECT value, raw_measurement_count, resolved_source, source_count, resolution "
            "FROM daily_metrics WHERE metric = 'sleep_hours' AND date = ?",
            (NIGHT,),
        ).fetchone()
        assert value == 6.0  # one source's night, not 12 h from adding two overlapping records of the same sleep
        assert (count, source_count, resolution) == (1, 2, "fallback")
        assert source in {WATCH, APP}
        assert invariant.verify(conn)["status"] == "ok"
    finally:
        conn.close()


def test_a_sleep_record_without_a_source_is_not_given_a_device_or_a_record_id(tmp_path, capsys):
    db = _import(tmp_path)
    capsys.readouterr()

    [(timestamp, value, source, importer, _)] = _sleep_rows(db, UNNAMED_NIGHT)
    assert (value, importer) == (7.0, "apple-health")
    # The documented stamp for a record with no source of its own is the importer's name, never a device.
    assert source == "apple-health"
    assert timestamp == f"{UNNAMED_NIGHT}T12:00:00"

    conn = sqlite3.connect(db)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(measurements)")}
    finally:
        conn.close()
    assert not {c for c in columns if "record_id" in c}  # the table stores no record id, so none can be invented


def test_an_interval_that_ends_before_it_starts_is_skipped_and_leaves_no_negative_source_row(tmp_path, capsys):
    export = tmp_path / "export.xml"
    export.write_text(
        '<?xml version="1.0"?><HealthData>'
        + _asleep("2026-03-01 00:00:00", "2026-03-01 06:00:00", WATCH)
        + _asleep("2026-03-01 07:00:00", "2026-03-01 06:00:00", APP)  # ends an hour before it starts
        + "</HealthData>",
        encoding="utf-8",
    )
    init_health_db(export, tmp_path / "health.db", replace=False, source="apple-health")
    captured = capsys.readouterr()

    assert "ends before it starts" in captured.err
    assert [(r[1], r[2]) for r in _sleep_rows(tmp_path / "health.db", NIGHT)] == [(6.0, WATCH)]


def test_reimporting_the_same_export_changes_nothing(tmp_path, capsys):
    db = _import(tmp_path)
    conn = sqlite3.connect(db)
    before = conn.execute("SELECT id, timestamp, metric, value, source, imported_at FROM measurements").fetchall()

    _import(tmp_path)
    out = capsys.readouterr().out

    assert "0 added, 0 updated" in out
    assert (
        conn.execute("SELECT id, timestamp, metric, value, source, imported_at FROM measurements").fetchall() == before
    )
    conn.close()


def test_a_database_imported_before_the_fix_is_corrected_in_place_by_a_reimport(tmp_path, capsys):
    db = tmp_path / "health.db"
    conn = connect_writable(db)
    ensure_schema(conn)
    # What the earlier adapter wrote for the night: both sources summed into one noon row under the importer.
    import_measurements(
        conn, "apple-health", [{"timestamp": f"{NIGHT}T12:00:00", "metric": "sleep_hours", "value": 12.0}]
    )
    conn.close()

    _import(tmp_path)
    capsys.readouterr()

    assert [(r[1], r[2]) for r in _sleep_rows(db, NIGHT)] == [(6.0, APP), (6.0, WATCH)]
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT value FROM daily_metrics WHERE metric = 'sleep_hours' AND date = ?", (NIGHT,)
        ).fetchone() == (6.0,)
        assert invariant.verify(conn)["status"] == "ok"
    finally:
        conn.close()
