"""
Source stamping (migration 10, logic.import_measurements): every imported measurement carries an
authoritative source, so the same day imported through two importers is resolved between them instead of
being summed as one unattributed group, and re-importing through one importer stays idempotent.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import invariant  # noqa: E402
from init_db import init_health_db  # noqa: E402
from logic import SCHEMA_VERSION, connect_writable, ensure_schema, import_measurements, insert_measurement  # noqa: E402

DAY = "2026-08-10"


def _conn(tmp_path):
    conn = connect_writable(tmp_path / "health.db")
    ensure_schema(conn)
    return conn


def _row(timestamp=f"{DAY}T12:00:00", metric="sleep_hours", value=6.2, **extra):
    return {"timestamp": timestamp, "metric": metric, "value": value, **extra}


def _projection(conn, metric="sleep_hours", day=DAY):
    found = conn.execute(
        "SELECT value, raw_measurement_count, resolved_source, source_count, resolution "
        "FROM daily_metrics WHERE metric = ? AND date = ?",
        (metric, day),
    ).fetchone()
    return dict(zip(("value", "count", "source", "sources", "resolution"), found, strict=True))


def _identities(conn):
    return conn.execute("SELECT id, imported_at FROM measurements ORDER BY id").fetchall()


def _measurements(conn, metric="sleep_hours"):
    return conn.execute(
        "SELECT importer, source, value FROM measurements WHERE metric = ? ORDER BY importer, source", (metric,)
    ).fetchall()


def test_two_importers_of_the_same_day_are_not_summed(tmp_path):
    conn = _conn(tmp_path)
    import_measurements(conn, "apple-health", [_row()])
    import_measurements(conn, "csv", [_row()])
    assert _measurements(conn) == [("apple-health", "apple-health", 6.2), ("csv", "csv", 6.2)]
    got = _projection(conn)
    assert (got["value"], got["count"], got["sources"], got["resolution"]) == (6.2, 1, 2, "fallback")
    assert got["source"] in {"apple-health", "csv"}
    assert invariant.verify(conn)["status"] == "ok"


def test_a_sum_metric_takes_one_importers_total_not_both(tmp_path):
    conn = _conn(tmp_path)
    import_measurements(conn, "csv", [_row(metric="steps", value=8000.0)])
    import_measurements(conn, "health-connect", [_row(metric="steps", value=8100.0)])
    got = _projection(conn, "steps")
    assert got["value"] in (8000.0, 8100.0) and got["sources"] == 2


def test_the_conflict_is_deterministic_and_rankable_by_importer_name(tmp_path):
    conn = _conn(tmp_path)
    import_measurements(conn, "apple-health", [_row(value=6.0)])
    import_measurements(conn, "csv", [_row(value=7.0)])
    first = _projection(conn)
    again = _projection(conn)
    assert first == again
    invariant.set_source_priority(conn, "sleep_hours", ["apple-health", "csv"])
    ranked = _projection(conn)
    assert (ranked["value"], ranked["source"], ranked["resolution"]) == (6.0, "apple-health", "priority")
    invariant.set_source_priority(conn, "sleep_hours", ["csv", "apple-health"])
    assert (_projection(conn)["value"], _projection(conn)["source"]) == (7.0, "csv")


def test_same_record_through_two_importers_stays_distinguishable(tmp_path):
    conn = _conn(tmp_path)
    import_measurements(conn, "apple-health", [_row()])
    import_measurements(conn, "csv", [_row()])
    rows = conn.execute("SELECT importer, source FROM measurements ORDER BY importer").fetchall()
    assert rows == [("apple-health", "apple-health"), ("csv", "csv")]


def test_reimporting_through_the_same_importer_is_idempotent(tmp_path):
    conn = _conn(tmp_path)
    batch = [_row(), _row(metric="steps", value=9000.0)]
    first = import_measurements(conn, "csv", batch)
    state = (_measurements(conn), _projection(conn), _identities(conn))
    second = import_measurements(conn, "csv", batch)
    assert (first["added"], first["unchanged"]) == (2, 0)
    assert (second["added"], second["updated"], second["unchanged"]) == (0, 0, 2)
    assert (_measurements(conn), _projection(conn), _identities(conn)) == state


def test_a_source_the_adapter_supplied_is_kept(tmp_path):
    conn = _conn(tmp_path)
    import_measurements(conn, "apple-health", [_row(metric="heart_rate", value=60.0, source="Ben's Apple Watch")])
    assert _measurements(conn, "heart_rate") == [("apple-health", "Ben's Apple Watch", 60.0)]


def test_an_empty_source_is_treated_as_missing(tmp_path):
    conn = _conn(tmp_path)
    import_measurements(conn, "csv", [_row(source="")])
    assert _measurements(conn) == [("csv", "csv", 6.2)]


def test_manual_measurements_are_not_stamped_or_touched(tmp_path):
    conn = _conn(tmp_path)
    insert_measurement(conn, f"{DAY}T08:00:00", "steps", 500.0)
    import_measurements(conn, "csv", [_row(metric="steps", value=8000.0)])
    manual = conn.execute("SELECT importer, source, value FROM measurements WHERE importer IS NULL").fetchall()
    assert manual == [(None, None, 500.0)]


def _legacy_database(tmp_path):
    """A version-9 database holding the double-counted state the old import path produced."""
    conn = _conn(tmp_path)
    invariant.drop_triggers(conn)
    for importer, value in (("apple-health", 6.2), ("csv", 6.2), ("csv", 8000.0)):
        metric = "steps" if value == 8000.0 else "sleep_hours"
        conn.execute(
            "INSERT INTO measurements (timestamp, metric, value, importer, imported_at) VALUES (?, ?, ?, ?, ?)",
            (f"{DAY}T12:00:00", metric, value, importer, "2026-09-01T08:00:00"),
        )
    conn.execute(
        "INSERT INTO measurements (timestamp, metric, value) VALUES (?, 'mood', 7)", (f"{DAY}T09:00:00",)
    )
    conn.commit()
    invariant.install_triggers(conn)
    invariant.repair(conn)
    conn.execute("PRAGMA user_version = 9")
    conn.commit()
    return conn


def test_migration_stamps_existing_rows_and_corrects_double_counted_days(tmp_path):
    conn = _legacy_database(tmp_path)
    assert _projection(conn)["value"] == 12.4  # the bug, as stored
    assert SCHEMA_VERSION >= 10
    ensure_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert _measurements(conn) == [("apple-health", "apple-health", 6.2), ("csv", "csv", 6.2)]
    got = _projection(conn)
    assert (got["value"], got["sources"], got["resolution"]) == (6.2, 2, "fallback")
    assert _projection(conn, "steps")["value"] == 8000.0
    assert _projection(conn, "steps")["resolution"] == "single"
    assert invariant.verify(conn)["status"] == "ok"


def test_migration_leaves_manual_rows_unstamped_and_is_idempotent(tmp_path):
    conn = _legacy_database(tmp_path)
    ensure_schema(conn)
    snapshot = conn.execute("SELECT id, importer, source, value FROM measurements ORDER BY id").fetchall()
    projection = conn.execute("SELECT date, metric, value, resolved_source FROM daily_metrics ORDER BY 1, 2").fetchall()
    conn.execute("PRAGMA user_version = 9")
    ensure_schema(conn)
    assert conn.execute("SELECT id, importer, source, value FROM measurements ORDER BY id").fetchall() == snapshot
    assert (
        conn.execute("SELECT date, metric, value, resolved_source FROM daily_metrics ORDER BY 1, 2").fetchall()
        == projection
    )
    assert conn.execute("SELECT source FROM measurements WHERE metric = 'mood'").fetchone() == (None,)


def test_migration_on_a_database_with_no_imported_rows_changes_nothing(tmp_path):
    conn = _conn(tmp_path)
    insert_measurement(conn, f"{DAY}T08:00:00", "steps", 500.0)
    conn.execute("PRAGMA user_version = 9")
    ensure_schema(conn)
    assert conn.execute("SELECT importer, source, value FROM measurements").fetchall() == [(None, None, 500.0)]
    assert _projection(conn, "steps")["value"] == 500.0


def test_sample_files_through_two_importers_no_longer_double_count(tmp_path):
    db = tmp_path / "health.db"
    sample = Path(__file__).resolve().parent.parent / "sample_data"
    init_health_db(sample / "apple_health_sample.xml", db, replace=False)
    init_health_db(sample / "health_sample.csv", db, replace=False)
    conn = sqlite3.connect(db)
    assert conn.execute(
        "SELECT value, resolution FROM daily_metrics WHERE metric = 'sleep_hours' AND date = ?", (DAY,)
    ).fetchone() == (6.2, "fallback")
    unstamped = "SELECT COUNT(*) FROM measurements WHERE source IS NULL AND importer IS NOT NULL"
    assert conn.execute(unstamped).fetchone() == (0,)
    # On every day both importers reported, the stored value is one importer's total, never their sum.
    two_sources = conn.execute(
        "SELECT date, value, resolved_source FROM daily_metrics WHERE metric = 'sleep_hours' AND source_count = 2"
    ).fetchall()
    assert two_sources
    for day, value, resolved in two_sources:
        totals = dict(
            conn.execute(
                "SELECT importer, SUM(value) FROM measurements WHERE metric = 'sleep_hours' AND date(timestamp) = ? "
                "GROUP BY importer",
                (day,),
            )
        )
        assert value == totals[resolved] and value < sum(totals.values())
    reimport = sqlite3.connect(db).execute("SELECT COUNT(*) FROM measurements").fetchone()
    init_health_db(sample / "apple_health_sample.xml", db, replace=False)
    init_health_db(sample / "health_sample.csv", db, replace=False)
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM measurements").fetchone() == reimport
