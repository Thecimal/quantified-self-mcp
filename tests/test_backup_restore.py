"""Backup and restore of the health database (db/backup.py), tested only against throwaway databases.

The premise: the database runs in WAL mode, so copying health.db alone while anything has it open loses
committed data. The online-backup API does not, and a restore must never overwrite a good database before
the backup it is restoring from has been validated.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

import logic
from db import backup
from init_db import init_health_db

REPO = Path(__file__).resolve().parent.parent
SAMPLE_CSV = REPO / "sample_data" / "health_sample.csv"
TABLES = ("measurements", "daily_metrics", "workout_sessions", "source_priority", "aggregation_rules", "imports")


def _make_db(path: Path) -> Path:
    init_health_db(SAMPLE_CSV, path, replace=False)
    with closing(logic.connect_writable(path)) as conn:
        conn.execute(
            "INSERT INTO workout_sessions (date, activity_type, duration_minutes, source) "
            "VALUES ('2026-08-10', 'run', 45, 'manual')"
        )
        conn.execute(
            "INSERT INTO measurements (timestamp, metric, value, source, importer) "
            "VALUES ('2026-08-24T08:00:00', 'steps', 4321, 'manual', NULL)"
        )
        conn.commit()
    return path


def _state(path: Path) -> dict:
    """Every table's rows (ids, provenance columns, timestamps and all) plus the schema version."""
    with logic.readonly_connection(path) as conn:
        out = {"user_version": conn.execute("PRAGMA user_version").fetchone()[0]}
        existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        for table in TABLES:
            if table in existing:
                out[table] = sorted(tuple(r) for r in conn.execute(f"SELECT * FROM {table}"))
    return out


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _integrity(path: Path) -> str:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute("PRAGMA integrity_check").fetchone()[0]


def _hidden_leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.glob(".*") if p.name not in {".", ".."})


@pytest.fixture
def db(tmp_path):
    return _make_db(tmp_path / "health.db")


def test_backup_is_logically_identical_while_another_connection_has_the_database_open(db, tmp_path):
    reader = sqlite3.connect(db)
    writer = logic.connect_writable(db)
    try:
        reader.execute("SELECT COUNT(*) FROM measurements").fetchone()  # a read in flight blocks checkpoints
        writer.execute(
            "INSERT INTO measurements (timestamp, metric, value, source) "
            "VALUES ('2026-10-01T08:00:00', 'steps', 1, 'm')"
        )
        writer.commit()
        assert Path(str(db) + "-wal").stat().st_size > 0  # the newest write is only in the write-ahead log
        facts = backup.create_backup(db, tmp_path / "b.db")
        live = _state(db)
    finally:
        reader.close()
        writer.close()
    copy = _state(tmp_path / "b.db")
    assert copy == live
    assert facts["measurements"] == len(live["measurements"])
    assert _integrity(tmp_path / "b.db") == "ok"
    assert sorted(p.name for p in tmp_path.glob("b.db*")) == ["b.db"]  # one self-contained file
    if os.name == "posix":
        assert (tmp_path / "b.db").stat().st_mode & 0o777 == 0o600


def test_a_plain_file_copy_of_the_database_is_not_a_backup(db, tmp_path):
    # The reason this module exists: with a reader holding the database open, committed rows live only in the
    # -wal file, so cp-ing health.db alone silently drops them.
    reader = sqlite3.connect(db)
    writer = logic.connect_writable(db)
    try:
        reader.execute("SELECT COUNT(*) FROM measurements").fetchone()
        writer.execute(
            "INSERT INTO measurements (timestamp, metric, value, source) "
            "VALUES ('2026-10-01T08:00:00', 'steps', 1, 'm')"
        )
        writer.commit()
        shutil.copy(db, tmp_path / "naive.db")
        live_count = writer.execute("SELECT COUNT(*) FROM measurements").fetchone()[0]
    finally:
        reader.close()
        writer.close()
    with closing(sqlite3.connect(tmp_path / "naive.db")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] < live_count


def test_backup_never_overwrites_and_leaves_no_partial_file(db, tmp_path):
    dest = tmp_path / "b.db"
    dest.write_bytes(b"an earlier backup")
    before = _sha(dest)
    with pytest.raises(backup.BackupError, match="already exists"):
        backup.create_backup(db, dest)
    assert _sha(dest) == before
    assert _hidden_leftovers(tmp_path) == []


def test_backup_of_a_missing_database_writes_nothing(tmp_path):
    with pytest.raises(backup.BackupError, match="nothing to back up"):
        backup.create_backup(tmp_path / "nope.db", tmp_path / "b.db")
    assert not (tmp_path / "b.db").exists()
    assert _hidden_leftovers(tmp_path) == []


def test_backup_of_a_damaged_database_writes_nothing(db, tmp_path):
    damaged = tmp_path / "damaged.db"
    damaged.write_bytes(db.read_bytes()[:20000])
    with pytest.raises(backup.BackupError):
        backup.create_backup(damaged, tmp_path / "b.db")
    assert not (tmp_path / "b.db").exists()
    assert _hidden_leftovers(tmp_path) == []


def test_restore_round_trip_recovers_the_exact_state_and_keeps_the_previous_database(db, tmp_path):
    original = _state(db)
    backup.create_backup(db, tmp_path / "b.db")
    with closing(logic.connect_writable(db)) as conn:  # damage the live data: lose all steps, add a stray row
        conn.execute("DELETE FROM measurements WHERE metric = 'steps'")
        conn.execute("INSERT INTO measurements (timestamp, metric, value) VALUES ('2026-11-01T00:00:00', 'mood', 1)")
        conn.commit()
    damaged_state = _state(db)
    assert damaged_state != original

    result = backup.restore_backup(tmp_path / "b.db", db)

    assert _state(db) == original  # provenance (importer, imported_at, source), ids and projection included
    assert len(_state(db)["measurements"]) == len(original["measurements"])  # no duplicated observations
    assert _integrity(db) == "ok"
    assert _state(Path(result["previous_database"])) == damaged_state  # nothing was destroyed
    assert _hidden_leftovers(tmp_path) == []
    # restoring again is idempotent
    backup.restore_backup(tmp_path / "b.db", db)
    assert _state(db) == original


def _newer_schema_copy(db: Path, target: Path) -> Path:
    shutil.copy(db, target)
    with closing(sqlite3.connect(target)) as conn:
        conn.execute(f"PRAGMA user_version = {logic.SCHEMA_VERSION + 1}")
        conn.commit()
    return target


def _other_sqlite_file(_db: Path, target: Path) -> Path:
    with closing(sqlite3.connect(target)) as conn:
        conn.execute("CREATE TABLE unrelated (x)")
        conn.commit()
    return target


def _bad_input(kind: str, db: Path, tmp_path: Path) -> Path:
    target = tmp_path / f"{kind}.bak"
    if kind == "garbage":
        target.write_bytes(os.urandom(5000))
    elif kind == "empty":
        target.write_bytes(b"")
    elif kind == "truncated":
        good = tmp_path / "good-for-truncation.db"
        backup.create_backup(db, good)
        target.write_bytes(good.read_bytes()[:20000])
    elif kind == "newer_schema":
        _newer_schema_copy(db, target)
    elif kind == "other_database":
        _other_sqlite_file(db, target)
    return target  # "missing": the path simply does not exist


@pytest.mark.parametrize("kind", ["garbage", "empty", "truncated", "newer_schema", "other_database", "missing"])
def test_restore_rejects_bad_input_and_leaves_the_current_database_untouched(db, tmp_path, kind):
    bad = _bad_input(kind, db, tmp_path)
    before = _sha(db)
    with pytest.raises(backup.BackupError):
        backup.restore_backup(bad, db)
    assert _sha(db) == before
    assert not list(tmp_path.glob("health.db.pre-restore-*"))
    assert _hidden_leftovers(tmp_path) == []


def test_restore_refuses_while_another_program_has_the_database_open(db, tmp_path):
    backup.create_backup(db, tmp_path / "b.db")
    before = _sha(db)
    other = sqlite3.connect(db)
    try:
        other.execute("SELECT COUNT(*) FROM measurements").fetchone()
        with pytest.raises(backup.BackupError, match="in use"):
            backup.restore_backup(tmp_path / "b.db", db)
    finally:
        other.close()
    assert _sha(db) == before
    assert not list(tmp_path.glob("health.db.pre-restore-*"))
    assert _hidden_leftovers(tmp_path) == []


def test_restore_replaces_a_damaged_database_and_keeps_its_files_aside(db, tmp_path):
    original = _state(db)
    backup.create_backup(db, tmp_path / "b.db")
    damaged_bytes = db.read_bytes()[:20000]
    db.write_bytes(damaged_bytes)
    for suffix in ("-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)

    result = backup.restore_backup(tmp_path / "b.db", db)

    assert _state(db) == original
    assert Path(result["previous_database"]).read_bytes() == damaged_bytes


def test_restore_onto_a_fresh_machine_needs_no_existing_database(db, tmp_path):
    original = _state(db)
    backup.create_backup(db, tmp_path / "b.db")
    target = tmp_path / "new-home" / "health.db"
    result = backup.restore_backup(tmp_path / "b.db", target)
    assert result["previous_database"] is None
    assert _state(target) == original


def test_a_backup_from_an_older_schema_is_accepted_and_upgrades_without_losing_data(tmp_path):
    old = tmp_path / "v6.db"
    with closing(sqlite3.connect(old)) as conn:
        for version, _description, migrate in logic.MIGRATIONS:
            if version > 6:
                break
            migrate(conn)
            conn.execute(f"PRAGMA user_version = {version}")
        for day in range(1, 11):
            conn.execute("INSERT INTO daily_metrics (date, steps) VALUES (?, ?)", (f"2026-08-{day:02d}", 9000 + day))
        conn.commit()
    target = tmp_path / "health.db"
    result = backup.restore_backup(old, target)
    assert result["user_version"] == 6
    with closing(logic.connect_writable(target)) as conn:
        logic.ensure_schema(conn)  # the normal upgrade on first open
        steps = conn.execute("SELECT COUNT(*) FROM daily_metrics WHERE metric = 'steps'").fetchone()[0]
    assert steps == 10


def test_encrypted_database_backup_stays_encrypted_and_round_trips(tmp_path, monkeypatch):
    pytest.importorskip("sqlcipher3")
    monkeypatch.setenv("HEALTH_DB_PASSPHRASE", "correct horse battery staple")
    enc = _make_db(tmp_path / "enc.db")
    assert enc.read_bytes()[:16] != b"SQLite format 3\x00"
    backup.create_backup(enc, tmp_path / "enc-backup.db")
    assert (tmp_path / "enc-backup.db").read_bytes()[:16] != b"SQLite format 3\x00"
    restored = tmp_path / "restored.db"
    backup.restore_backup(tmp_path / "enc-backup.db", restored)
    assert _state(restored) == _state(enc)
    monkeypatch.delenv("HEALTH_DB_PASSPHRASE")
    with pytest.raises(backup.BackupError, match="HEALTH_DB_PASSPHRASE"):
        backup.restore_backup(tmp_path / "enc-backup.db", tmp_path / "wrong.db")
    assert not (tmp_path / "wrong.db").exists()


def _cli(*args, db_path, cwd=REPO):
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "HEALTH_DB_PASSPHRASE"}}
    env["HEALTH_DB_PATH"] = str(db_path)
    return subprocess.run(
        [sys.executable, "-m", "db.backup", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=120
    )


def test_cli_create_and_restore_round_trip(db, tmp_path):
    original = _state(db)
    created = _cli("create", str(tmp_path / "cli.bak"), db_path=db)
    assert created.returncode == 0, created.stderr
    assert "Backup written" in created.stdout
    restored = _cli("restore", str(tmp_path / "cli.bak"), db_path=tmp_path / "elsewhere.db")
    assert restored.returncode == 0, restored.stderr
    assert _state(tmp_path / "elsewhere.db") == original


def test_cli_reports_a_bad_backup_without_a_traceback_and_without_touching_the_database(db, tmp_path):
    (tmp_path / "junk.bak").write_bytes(b"not a database" * 100)
    before = _sha(db)
    result = _cli("restore", str(tmp_path / "junk.bak"), db_path=db)
    assert result.returncode == 1
    assert result.stderr.startswith("Error:") and "Traceback" not in result.stderr
    assert _sha(db) == before


def _fail_replace_of(monkeypatch, name_prefix: str):
    real_replace = os.replace

    def flaky(src, dst, *args, **kwargs):
        if Path(src).name.startswith(name_prefix):
            raise OSError("simulated failure (disk full, permissions, power loss)")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(backup.os, "replace", flaky)


def test_a_restore_interrupted_at_the_final_swap_puts_the_previous_database_back(db, tmp_path, monkeypatch):
    backup.create_backup(db, tmp_path / "b.db")
    before_state = _state(db)
    _fail_replace_of(monkeypatch, ".health.db.restore-")
    with pytest.raises(backup.BackupError, match="previous database was kept"):
        backup.restore_backup(tmp_path / "b.db", db)
    monkeypatch.undo()
    assert _state(db) == before_state and _integrity(db) == "ok"
    assert not list(tmp_path.glob("health.db.pre-restore-*"))
    assert _hidden_leftovers(tmp_path) == []


def test_a_backup_interrupted_at_the_final_rename_leaves_nothing_behind(db, tmp_path, monkeypatch):
    _fail_replace_of(monkeypatch, ".b.db.partial-")
    with pytest.raises(OSError, match="simulated failure"):
        backup.create_backup(db, tmp_path / "b.db")
    monkeypatch.undo()
    assert not (tmp_path / "b.db").exists()
    assert _hidden_leftovers(tmp_path) == []
