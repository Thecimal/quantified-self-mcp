"""
Health Connect import: retrying a run that failed part-way.

Contract being checked (db/invariant.bulk_insert_measurements, logic.import_measurements, init_db.init_health_db):
- the deletes and inserts of one import share one transaction, so a failure inside it rolls back and leaves the
  stored rows and daily_metrics exactly as they were; the run is recorded as failed;
- the rows are committed *before* daily_metrics is rebuilt (a separate transaction), so a failure in the rebuild
  leaves the rows stored and the run recorded as failed;
- re-running the same import never duplicates or rewrites stored rows, and always ends with daily_metrics matching
  the measurements, with the run recorded as succeeded only when that holds.

The records are synthetic and fixed. Failures are injected with monkeypatch at the two points above.
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db.invariant as invariant  # noqa: E402
import logic  # noqa: E402
from init_db import init_health_db  # noqa: E402


def _steps(day, count, record_id):
    return {
        "recordType": "StepsRecord",
        "startTime": f"2026-06-{day:02d}T08:00:00Z",
        "endTime": f"2026-06-{day:02d}T08:10:00Z",
        "count": count,
        "metadata": {"id": record_id},
    }


FIRST = [_steps(1, 1000, "a1"), _steps(2, 2000, "a2")]
SECOND = [*FIRST, _steps(3, 3000, "a3"), _steps(4, 4000, "a4")]  # the same export, two days later
EDITED = [_steps(1, 1000, "a1"), _steps(2, 2500, "a2")]  # day 2 changed, so the import must replace it


def _import(tmp_path, records, name):
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(records), encoding="utf-8")
    init_health_db(path, tmp_path / "health.db", replace=False, source="health-connect")


def _state(tmp_path):
    with sqlite3.connect(tmp_path / "health.db") as conn:
        return {
            "measurements": conn.execute(
                "SELECT id, timestamp, metric, value, source, importer, imported_at FROM measurements ORDER BY id"
            ).fetchall(),
            "daily": conn.execute("SELECT date, value FROM daily_metrics ORDER BY date").fetchall(),
            "runs": conn.execute(
                "SELECT status, records_seen, records_added, records_updated, records_unchanged "
                "FROM imports ORDER BY id"
            ).fetchall(),
            "verify": invariant.verify(conn)["status"],
            "triggers": conn.execute("SELECT count(*) FROM sqlite_master WHERE type = 'trigger'").fetchone()[0],
        }


def _boom(*_args, **_kwargs):
    raise sqlite3.OperationalError("disk I/O error")


def test_a_failure_in_the_rebuild_leaves_rows_stored_and_a_retry_repairs_the_projection(tmp_path, monkeypatch):
    _import(tmp_path, FIRST, "first")
    first = _state(tmp_path)
    assert first["verify"] == "ok" and len(first["measurements"]) == 2
    assert first["daily"] == [("2026-06-01", 1000.0), ("2026-06-02", 2000.0)]

    # The second import commits its rows, then fails while rebuilding daily_metrics.
    with monkeypatch.context() as patch:
        patch.setattr(invariant, "repair", _boom)
        with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
            _import(tmp_path, SECOND, "second")
    failed = _state(tmp_path)
    assert failed["measurements"][:2] == first["measurements"]  # what was committed before is intact
    assert len(failed["measurements"]) == 4  # the new rows were committed (documented: rows, then rebuild)
    assert failed["runs"][-1][0] == "failed" and failed["runs"][0] == first["runs"][0]
    assert failed["verify"] == "mismatch"  # daily_metrics is behind; the failed status says so
    assert failed["triggers"] == first["triggers"]  # the write-path triggers are back

    # Retrying the same import: nothing new to write, but it must leave the database consistent.
    _import(tmp_path, SECOND, "second")
    retried = _state(tmp_path)
    assert retried["measurements"] == failed["measurements"]  # no duplicates, no rewrites, provenance untouched
    assert {(m[4], m[5]) for m in retried["measurements"]} == {("health-connect", "health-connect")}
    assert retried["daily"] == [
        ("2026-06-01", 1000.0),
        ("2026-06-02", 2000.0),
        ("2026-06-03", 3000.0),
        ("2026-06-04", 4000.0),
    ]
    assert retried["verify"] == "ok"
    assert retried["runs"][-1] == ("succeeded", 4, 0, 0, 4)  # 4 seen, 0 written, 4 already stored


def test_a_failure_inside_the_write_rolls_back_and_a_retry_applies_the_change_once(tmp_path, monkeypatch):
    _import(tmp_path, FIRST, "first")
    before = _state(tmp_path)

    # The edited day 2 must be purged and re-inserted in one transaction; fail on the purge.
    with monkeypatch.context() as patch:
        patch.setattr(logic, "_PURGE_STALE_DAY_SQL", "DELETE FROM no_such_table WHERE importer = :importer")
        with pytest.raises(sqlite3.OperationalError):
            _import(tmp_path, EDITED, "edited")
    failed = _state(tmp_path)
    assert failed["measurements"] == before["measurements"]  # rolled back: rows, ids and imported_at unchanged
    assert failed["daily"] == before["daily"] and failed["verify"] == "ok"
    assert failed["triggers"] == before["triggers"]
    assert failed["runs"][-1][0] == "failed"

    _import(tmp_path, EDITED, "edited")
    retried = _state(tmp_path)
    assert [(m[1][:10], m[3]) for m in retried["measurements"]] == [("2026-06-01", 1000.0), ("2026-06-02", 2500.0)]
    assert retried["measurements"][0] == before["measurements"][0]  # the unchanged day was not touched
    assert retried["daily"] == [("2026-06-01", 1000.0), ("2026-06-02", 2500.0)] and retried["verify"] == "ok"
    assert retried["runs"][-1] == ("succeeded", 2, 0, 1, 1)  # one day replaced, one left alone
