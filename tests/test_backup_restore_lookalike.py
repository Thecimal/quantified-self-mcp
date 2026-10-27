"""
Restoring a backup: a file that only looks like a health database must be rejected, and a real one restored exactly.

db/backup.py validates a backup before it touches the current database. It used to accept any intact SQLite file
that had a table *named* measurements or daily_metrics, so an unrelated database (or a damaged export) with such a
table replaced the current database, which was moved aside, and the server could not open the result. The checks
here use throwaway databases and synthetic readings only.
"""

import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logic  # noqa: E402
from db import backup, invariant  # noqa: E402

READINGS = [  # timestamp, metric, value, unit, source, source_type, importer, imported_at
    ("2026-08-01T07:30:00", "steps", 4200.0, "count", "watch", "wearable", "apple-health", "2026-08-02T09:00:00"),
    ("2026-08-01T18:00:00", "steps", 3100.0, "count", "watch", "wearable", "apple-health", "2026-08-02T09:00:00"),
    ("2026-08-01T07:00:00", "weight_kg", 70.5, "kg", "scale", "device", None, None),
    ("2026-08-02T06:45:00", "sleep_hours", 7.25, "h", "manual", "manual", None, None),
]


def _make_database(path: Path) -> Path:
    with closing(logic.connect_writable(path)) as conn:
        logic.ensure_schema(conn)
        conn.executemany(
            "INSERT INTO measurements (timestamp, metric, value, unit, source, source_type, importer, imported_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            READINGS,
        )
        conn.commit()
    return path


def _rows(path: Path):
    with logic.readonly_connection(path) as conn:
        return {
            "measurements": conn.execute(
                "SELECT id, timestamp, metric, value, unit, source, source_type, importer, imported_at "
                "FROM measurements ORDER BY id"
            ).fetchall(),
            "daily": conn.execute(
                "SELECT date, metric, value, resolved_source FROM daily_metrics ORDER BY 1, 2"
            ).fetchall(),
            "verify": invariant.verify(conn)["status"],
        }


def _look_alike(path: Path, table: str, columns: str) -> Path:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(f"CREATE TABLE {table} ({columns})")
        conn.execute(f"INSERT INTO {table} VALUES (1, 'x')")
        conn.commit()
    return path


def test_a_valid_backup_restores_every_reading_with_its_provenance(tmp_path):
    original = _make_database(tmp_path / "original.db")
    snapshot = tmp_path / "backup.db"
    backup.create_backup(original, snapshot)

    restored = tmp_path / "restored" / "health.db"  # a fresh machine: nothing to replace
    facts = backup.restore_backup(snapshot, restored)

    assert facts["measurements"] == len(READINGS) and facts["previous_database"] is None
    before, after = _rows(original), _rows(restored)
    assert after == before  # ids, timestamps, values, units, source, source_type, importer, imported_at, projection
    assert [row[1:] for row in after["measurements"]] == [
        (ts, metric, value, unit, source, source_type, importer, imported_at)
        for ts, metric, value, unit, source, source_type, importer, imported_at in READINGS
    ]
    assert after["verify"] == "ok"
    with closing(logic.connect_writable(restored)) as conn:  # the server's own first open accepts it
        logic.ensure_schema(conn)


@pytest.mark.parametrize(
    ("table", "columns"),
    [
        ("measurements", "id INTEGER, label TEXT"),  # a table that only borrows the name
        ("daily_metrics", "id INTEGER, label TEXT"),
    ],
)
def test_a_file_that_only_looks_like_a_health_database_is_rejected_and_the_current_one_survives(
    tmp_path, table, columns
):
    current = _make_database(tmp_path / "health.db")
    with closing(sqlite3.connect(current)) as conn:
        conn.execute("PRAGMA journal_mode = DELETE")  # one file, so its bytes can be compared
    before_bytes, before_rows = current.read_bytes(), _rows(current)
    impostor = _look_alike(tmp_path / "other-app.db", table, columns)

    with pytest.raises(backup.BackupError, match=f"{table} table without the column"):
        backup.restore_backup(impostor, current)

    assert current.read_bytes() == before_bytes  # not touched, not even rewritten
    assert _rows(current) == before_rows
    assert not list(tmp_path.glob("health.db.pre-restore-*"))  # nothing was moved aside
    assert sorted(p.name for p in tmp_path.glob(".*")) == []  # no staged leftovers
