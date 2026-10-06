"""
Incremental imports: unchanged (metric, day)s are left alone, new and changed ones are written, --replace
removals are counted, and every run is audited in `imports` with its coverage before and after.
"""

import sqlite3
import sys
from pathlib import Path

import pytest
from fastmcp import Client

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from init_db import init_health_db  # noqa: E402
from logic import (  # noqa: E402
    SCHEMA_VERSION,
    connect_writable,
    dataset_coverage,
    ensure_schema,
    import_measurements,
    list_imports,
    upsert_daily_metric_measurements,
)


def _conn(tmp_path):
    conn = connect_writable(tmp_path / "health.db")
    ensure_schema(conn)
    return conn


def _row(day, value, metric="steps", hour=12, **extra):
    return {"timestamp": f"2026-08-{day:02d}T{hour:02d}:00:00", "metric": metric, "value": value, **extra}


def _counts(stats):
    return tuple(stats[k] for k in ("seen", "added", "updated", "unchanged", "removed"))


def _stored(conn):
    return conn.execute("SELECT id, timestamp, value, imported_at FROM measurements ORDER BY id").fetchall()


def _daily(conn):
    return dict(conn.execute("SELECT date, value FROM daily_metrics WHERE metric = 'steps'").fetchall())


def test_a_first_import_adds_everything(tmp_path):
    conn = _conn(tmp_path)
    stats = import_measurements(conn, "csv", [_row(1, 100), _row(2, 200), _row(3, 300)])
    assert _counts(stats) == (3, 3, 0, 0, 0)
    assert stats["verify"]["status"] == "ok"


def test_importing_the_same_rows_again_changes_nothing_not_even_ids_or_timestamps(tmp_path):
    conn = _conn(tmp_path)
    rows = [_row(1, 100), _row(2, 200), _row(3, 300)]
    import_measurements(conn, "csv", rows)
    before = _stored(conn)
    stats = import_measurements(conn, "csv", rows)
    assert _counts(stats) == (3, 0, 0, 3, 0)
    assert _stored(conn) == before
    assert import_measurements(conn, "csv", rows)["unchanged"] == 3
    assert conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 3


def test_overlapping_imports_write_only_the_new_days(tmp_path):
    conn = _conn(tmp_path)
    import_measurements(conn, "csv", [_row(d, d * 100) for d in range(1, 6)])
    before = {row[1]: row for row in _stored(conn)}
    stats = import_measurements(conn, "csv", [_row(d, d * 100) for d in range(4, 9)])
    assert _counts(stats) == (5, 3, 0, 2, 0)
    after = {row[1]: row for row in _stored(conn)}
    assert set(after) == {f"2026-08-{d:02d}T12:00:00" for d in range(1, 9)}
    assert all(after[ts] == before[ts] for ts in before)  # days 1-5 untouched, ids and imported_at included
    assert _daily(conn)["2026-08-08"] == 800


def test_a_corrected_value_replaces_only_that_day(tmp_path):
    conn = _conn(tmp_path)
    import_measurements(conn, "csv", [_row(1, 100), _row(2, 200), _row(3, 300)])
    before = {row[1]: row for row in _stored(conn)}
    stats = import_measurements(conn, "csv", [_row(1, 100), _row(2, 250), _row(3, 300)])
    assert _counts(stats) == (3, 0, 1, 2, 0)
    after = {row[1]: row for row in _stored(conn)}
    assert after["2026-08-01T12:00:00"] == before["2026-08-01T12:00:00"]
    assert after["2026-08-03T12:00:00"] == before["2026-08-03T12:00:00"]
    changed, was = after["2026-08-02T12:00:00"], before["2026-08-02T12:00:00"]
    assert changed[2] == 250 and changed[0] != was[0]
    assert _daily(conn) == {"2026-08-01": 100.0, "2026-08-02": 250.0, "2026-08-03": 300.0}
    assert conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 3


def test_a_day_with_several_readings_is_compared_as_a_whole(tmp_path):
    conn = _conn(tmp_path)
    morning, evening = _row(1, 60, "heart_rate", 8), _row(1, 80, "heart_rate", 20)
    import_measurements(conn, "csv", [morning, evening])
    assert import_measurements(conn, "csv", [evening, morning])["unchanged"] == 2  # order does not matter
    stats = import_measurements(conn, "csv", [morning, _row(1, 85, "heart_rate", 20)])
    assert _counts(stats) == (2, 0, 2, 0, 0)
    assert sorted(r[0] for r in conn.execute("SELECT value FROM measurements")) == [60, 85]


def test_a_changed_unit_or_source_counts_as_a_change(tmp_path):
    conn = _conn(tmp_path)
    import_measurements(conn, "csv", [_row(1, 100, source="Watch")])
    assert import_measurements(conn, "csv", [_row(1, 100, source="Watch")])["unchanged"] == 1
    assert import_measurements(conn, "csv", [_row(1, 100, source="Phone")])["updated"] == 1
    assert conn.execute("SELECT source FROM measurements").fetchall() == [("Phone",)]


def test_replace_counts_what_it_removes_and_keeps_what_is_unchanged(tmp_path):
    conn = _conn(tmp_path)
    import_measurements(conn, "csv", [_row(1, 100), _row(2, 200), _row(3, 300)])
    stats = import_measurements(conn, "csv", [_row(1, 100), _row(2, 250)], replace=True)
    assert _counts(stats) == (2, 0, 1, 1, 1)
    assert _daily(conn) == {"2026-08-01": 100.0, "2026-08-02": 250.0}
    emptied = import_measurements(conn, "csv", [], replace=True)
    assert _counts(emptied) == (0, 0, 0, 0, 2) and _daily(conn) == {}


def test_without_replace_days_missing_from_the_source_are_kept(tmp_path):
    conn = _conn(tmp_path)
    import_measurements(conn, "csv", [_row(1, 100), _row(2, 200)])
    stats = import_measurements(conn, "csv", [_row(1, 100)])
    assert _counts(stats) == (1, 0, 0, 1, 0)
    assert set(_daily(conn)) == {"2026-08-01", "2026-08-02"}


def test_other_importers_and_manual_entries_are_never_touched(tmp_path):
    conn = _conn(tmp_path)
    upsert_daily_metric_measurements(conn, "2026-08-01", {"mood": 4})
    import_measurements(conn, "apple-health", [_row(1, 9000, source="Watch")])
    other = _stored(conn)
    stats = import_measurements(conn, "csv", [_row(1, 100)], replace=True)
    assert _counts(stats) == (1, 1, 0, 0, 0)
    assert all(row in _stored(conn) for row in other)
    assert conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == len(other) + 1


def test_an_unsupported_metric_is_rejected_before_anything_is_compared_or_changed(tmp_path):
    conn = _conn(tmp_path)
    import_measurements(conn, "csv", [_row(1, 100)])
    before = _stored(conn)
    with pytest.raises(ValueError, match="no aggregation_rules entry"):
        import_measurements(conn, "csv", [_row(1, 999), _row(2, 1, "not_a_metric")], replace=True)
    assert _stored(conn) == before


def test_a_failure_while_inserting_changes_nothing(tmp_path):
    conn = _conn(tmp_path)
    import_measurements(conn, "csv", [_row(1, 100), _row(2, 200)])
    before = _stored(conn)
    with pytest.raises(sqlite3.IntegrityError):
        import_measurements(conn, "csv", [_row(1, 150), _row(2, None)])
    assert _stored(conn) == before
    assert _daily(conn) == {"2026-08-01": 100.0, "2026-08-02": 200.0}


def test_dataset_coverage_is_the_earliest_and_latest_day(tmp_path):
    conn = _conn(tmp_path)
    assert dataset_coverage(conn) == (None, None)
    import_measurements(conn, "csv", [_row(5, 1), _row(2, 1), _row(9, 1, "mood")])
    assert dataset_coverage(conn) == ("2026-08-02", "2026-08-09")


def _csv(tmp_path, rows, name="health.csv"):
    path = tmp_path / name
    path.write_text("date,steps\n" + "".join(f"{d},{s}\n" for d, s in rows), encoding="utf-8")
    return path


def _imports(db):
    conn = connect_writable(db)
    try:
        return list(reversed(list_imports(conn, limit=10)))
    finally:
        conn.close()


def test_each_run_is_audited_with_counts_and_coverage_before_and_after(tmp_path, capsys):
    db = tmp_path / "health.db"
    first = _csv(tmp_path, [("2026-01-01", 100), ("2026-01-02", 200), ("2026-01-03", 300)])
    init_health_db(first, db, replace=False)
    assert "Import: 3 added, 0 updated, 0 unchanged, 0 removed." in capsys.readouterr().out

    second = _csv(tmp_path, [("2026-01-01", 100), ("2026-01-02", 250), ("2026-01-03", 300), ("2026-01-04", 400)])
    init_health_db(second, db, replace=False)
    assert "Import: 1 added, 1 updated, 2 unchanged, 0 removed." in capsys.readouterr().out
    init_health_db(second, db, replace=False)

    one, two, three = _imports(db)
    assert (one["records_seen"], one["records_added"], one["records_unchanged"]) == (3, 3, 0)
    assert (one["coverage_before_start"], one["coverage_before_end"]) == (None, None)
    assert (one["coverage_after_start"], one["coverage_after_end"]) == ("2026-01-01", "2026-01-03")
    assert (two["records_seen"], two["records_added"], two["records_updated"], two["records_unchanged"]) == (4, 1, 1, 2)
    assert two["measurements_written"] == 2 and two["records_removed"] == 0
    assert (two["coverage_before_end"], two["coverage_after_end"]) == ("2026-01-03", "2026-01-04")
    assert (three["records_added"], three["records_updated"], three["records_unchanged"]) == (0, 0, 4)
    assert three["measurements_written"] == 0 and three["status"] == "succeeded"
    assert one["source_sha256"] != two["source_sha256"] and two["source_sha256"] == three["source_sha256"]


def test_replace_through_the_cli_path_records_the_removals(tmp_path):
    db = tmp_path / "health.db"
    init_health_db(_csv(tmp_path, [("2026-01-01", 1), ("2026-01-02", 2)]), db, replace=False)
    init_health_db(_csv(tmp_path, [("2026-01-01", 1)], "later.csv"), db, replace=True)
    _, run = _imports(db)
    assert (run["records_unchanged"], run["records_removed"]) == (1, 1)
    assert (run["coverage_before_end"], run["coverage_after_end"]) == ("2026-01-02", "2026-01-01")


def test_a_database_from_before_the_audit_columns_is_migrated_in_place(tmp_path):
    conn = _conn(tmp_path)
    conn.execute("DROP TABLE imports")
    conn.execute(
        "CREATE TABLE imports (id INTEGER PRIMARY KEY AUTOINCREMENT, importer TEXT NOT NULL, source_file TEXT, "
        "source_sha256 TEXT, status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')), "
        "started_at TEXT NOT NULL, finished_at TEXT, rows_loaded INTEGER, rows_skipped INTEGER, "
        "measurements_written INTEGER, error TEXT)"
    )
    conn.execute(
        "INSERT INTO imports (importer, source_file, status, started_at, rows_loaded) "
        "VALUES ('csv', 'old.csv', 'succeeded', '2026-09-01T08:00:00', 7)"
    )
    conn.execute("PRAGMA user_version = 8")
    conn.commit()
    ensure_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION >= 9
    columns = {row[1] for row in conn.execute("PRAGMA table_info(imports)")}
    assert {"records_seen", "records_added", "records_removed", "coverage_after_end"} <= columns
    (old,) = list_imports(conn)
    assert (old["source_file"], old["rows_loaded"], old["records_seen"]) == ("old.csv", 7, None)


@pytest.fixture
def health_db(tmp_path, monkeypatch):
    monkeypatch.setenv("HEALTH_DB_PATH", str(tmp_path / "health.db"))
    for mod in ("server", "privacy", "tools.health", "tools.measurements", "tools.workouts", "tools.status"):
        sys.modules.pop(mod, None)
    import server

    return server


async def test_get_import_status_returns_the_audit_fields(health_db, tmp_path):
    source = _csv(tmp_path, [("2026-01-01", 1), ("2026-01-02", 2)])
    init_health_db(source, health_db.HEALTH_DB_PATH, replace=False)
    init_health_db(source, health_db.HEALTH_DB_PATH, replace=False)
    async with Client(health_db.mcp) as client:
        newest, first = (await client.call_tool("get_import_status", {})).structured_content["imports"]
    assert (first["records_added"], first["records_unchanged"]) == (2, 0)
    assert (newest["records_added"], newest["records_unchanged"], newest["records_removed"]) == (0, 2, 0)
    assert newest["coverage_before_end"] == newest["coverage_after_end"] == "2026-01-02"
