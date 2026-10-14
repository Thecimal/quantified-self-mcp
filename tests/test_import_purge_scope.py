"""
The delete that replaces a changed (metric, day) during an incremental import must seek the day, not scan the metric.

It used to filter on substr(timestamp, 1, 10), which no index serves, so every stale key read every row of its
metric. These tests pin the access path (query plan, plus a VM-step bound that does not depend on wall-clock time)
and that exactly the same rows are deleted as before, including at day, month and year boundaries.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logic import (  # noqa: E402
    _PURGE_STALE_DAY_SQL,
    _prefix_upper_bound,
    connect_writable,
    ensure_schema,
    import_measurements,
)

OLD_SQL = "DELETE FROM measurements WHERE importer = :importer AND metric = :metric AND substr(timestamp, 1, 10) = :day"


def _db(tmp_path, name):
    conn = connect_writable(tmp_path / name)
    ensure_schema(conn)
    return conn


def _insert(conn, ts, metric="steps", importer="a", value=1.0):
    conn.execute(
        "INSERT INTO measurements (timestamp, metric, value, importer, source) VALUES (?, ?, ?, ?, 'S')",
        (ts, metric, value, importer),
    )


def test_the_purge_statement_seeks_the_day_through_the_timestamp_index(tmp_path):
    conn = _db(tmp_path, "plan.db")
    params = {"importer": "a", "metric": "steps", "day": "2026-08-10", "upper": "2026-08-11"}
    detail = " ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + _PURGE_STALE_DAY_SQL, params))
    assert "idx_measurements_metric_timestamp" in detail
    assert "timestamp>?" in detail and "timestamp<?" in detail, detail
    old = " ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + OLD_SQL, {k: params[k] for k in OLD_PARAMS}))
    assert "timestamp>?" not in old  # the shape being replaced: a scan of the whole metric range


OLD_PARAMS = ("importer", "metric", "day")


def _vm_steps(conn, sql, params):
    steps = [0]

    def tick():
        steps[0] += 1
        return 0

    conn.set_progress_handler(tick, 1)
    try:
        conn.execute(sql, params)
    finally:
        conn.set_progress_handler(None, 0)
    return steps[0]


def test_purging_one_day_does_not_scale_with_the_size_of_the_metric(tmp_path):
    new_db, old_db = _db(tmp_path, "new.db"), _db(tmp_path, "old.db")
    for conn in (new_db, old_db):
        for trigger in ("ai", "au", "ad"):  # as in the bulk import path: only the delete itself is measured
            conn.execute(f"DROP TRIGGER IF EXISTS trg_measurements_{trigger}")
        for day in range(1, 29):
            for hour in range(24):
                _insert(conn, f"2026-02-{day:02d}T{hour:02d}:00:00")
        conn.commit()
    params = {"importer": "a", "metric": "steps", "day": "2026-02-27", "upper": "2026-02-28"}
    new_steps = _vm_steps(new_db, _PURGE_STALE_DAY_SQL, params)
    old_steps = _vm_steps(old_db, OLD_SQL, {k: params[k] for k in OLD_PARAMS})
    assert new_db.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 28 * 24 - 24
    assert new_steps * 5 < old_steps, (new_steps, old_steps)


BOUNDARY_TIMESTAMPS = [
    "2026-08-09T23:59:59",
    "2026-08-10",
    "2026-08-10 07:30:00",
    "2026-08-10T00:00:00",
    "2026-08-10T23:59:59.999",
    "2026-08-11T00:00:00",
    "2026-08-31T23:59:59",
    "2026-09-01T00:00:00",
    "2026-12-31T12:00:00",
    "2027-01-01T00:00:00",
]


@pytest.mark.parametrize("day", ["2026-08-10", "2026-08-31", "2026-09-01", "2026-12-31", "2027-01-01"])
def test_the_rows_deleted_are_exactly_those_the_old_filter_deleted(tmp_path, day):
    new_db, old_db = _db(tmp_path, "new.db"), _db(tmp_path, "old.db")
    upper = _prefix_upper_bound(day)
    for conn in (new_db, old_db):
        for ts in BOUNDARY_TIMESTAMPS:
            _insert(conn, ts)  # the importer's own rows
            _insert(conn, ts, importer="b")  # another importer: untouched
            _insert(conn, ts, metric="heart_rate")  # another metric: untouched
            _insert(conn, ts, importer=None)  # a manual measurement: untouched
        conn.commit()
    new_db.execute(_PURGE_STALE_DAY_SQL, {"importer": "a", "metric": "steps", "day": day, "upper": upper})
    old_db.execute(OLD_SQL, {"importer": "a", "metric": "steps", "day": day})

    def remaining(conn):
        return conn.execute("SELECT timestamp, metric, importer FROM measurements ORDER BY 1, 2, 3").fetchall()

    assert remaining(new_db) == remaining(old_db)
    deleted = new_db.execute(
        "SELECT COUNT(*) FROM measurements WHERE importer = 'a' AND metric = 'steps' AND substr(timestamp, 1, 10) = ?",
        (day,),
    ).fetchone()[0]
    assert deleted == 0 and any(ts.startswith(day) for ts in BOUNDARY_TIMESTAMPS)


@pytest.mark.parametrize("day", ["2026-08-10", "2026-12-31", "x", "2026", "2026-09-31", "é"])
def test_the_upper_bound_is_above_every_string_that_starts_with_the_day_and_not_above_the_next_one(day):
    upper = _prefix_upper_bound(day)
    assert upper is not None and upper > day
    for tail in ("", " ", "T00:00:00", "T23:59:59.999", "\uffff", "~"):
        assert day <= day + tail < upper
    assert upper == day[:-1] + chr(ord(day[-1]) + 1)


@pytest.mark.parametrize("day", ["", chr(0xD7FF), chr(0x10FFFF)])
def test_a_key_with_no_safe_upper_bound_falls_back_to_the_unbounded_delete(day):
    assert _prefix_upper_bound(day) is None


def test_import_replaces_a_changed_day_and_only_that_day_end_to_end(tmp_path):
    conn = _db(tmp_path, "e2e.db")

    def rows(day, value):
        return [{"timestamp": f"2026-08-{day:02d}T{h:02d}:00:00", "metric": "steps", "value": value} for h in (8, 20)]

    first = rows(9, 1) + rows(10, 2) + rows(11, 3)
    import_measurements(conn, "a", first)
    import_measurements(conn, "b", rows(10, 50))
    conn.execute(
        "INSERT INTO measurements (timestamp, metric, value, source) VALUES ('2026-08-10T09:00:00', 'steps', 7, 'M')"
    )
    conn.commit()

    stats = import_measurements(conn, "a", rows(9, 1) + rows(10, 20) + rows(11, 3))
    assert (stats["added"], stats["updated"], stats["unchanged"]) == (0, 2, 4)
    got = conn.execute(
        "SELECT substr(timestamp, 1, 10), importer, value FROM measurements ORDER BY 1, 2, 3, timestamp"
    ).fetchall()
    assert got == [
        ("2026-08-09", "a", 1.0), ("2026-08-09", "a", 1.0),
        ("2026-08-10", None, 7.0),
        ("2026-08-10", "a", 20.0), ("2026-08-10", "a", 20.0),
        ("2026-08-10", "b", 50.0), ("2026-08-10", "b", 50.0),
        ("2026-08-11", "a", 3.0), ("2026-08-11", "a", 3.0),
    ]  # fmt: skip
    assert stats["verify"]["status"] == "ok"
    removed = import_measurements(conn, "a", rows(9, 1), replace=True)
    assert removed["removed"] == 4
    assert conn.execute("SELECT COUNT(*) FROM measurements WHERE importer = 'a'").fetchone()[0] == 2
