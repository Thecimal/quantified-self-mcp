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
    DB_PASSPHRASE_ENV,
    MIGRATIONS,
    SCHEMA_VERSION,
    connect_writable,
    db_error_types,
    default_data_dir,
    encryption_available,
    ensure_schema,
    numeric_stats,
    parse_date,
    readonly_connection,
    resolve_range,
    row_class,
    upsert_metrics,
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


def test_ensure_schema_creates_table_with_all_columns():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(daily_metrics)")}
    assert columns == {
        "date",
        "steps",
        "sleep_hours",
        "resting_heart_rate",
        *ADDED_COLUMNS,
    }


def test_ensure_schema_migrates_older_table_missing_new_columns():
    conn = sqlite3.connect(":memory:")
    # Simulate a database created before weight/workout/mood/water existed.
    conn.executescript(
        """
        CREATE TABLE daily_metrics (
            date TEXT PRIMARY KEY,
            steps INTEGER,
            sleep_hours REAL,
            resting_heart_rate INTEGER
        );
        """
    )
    conn.execute(
        "INSERT INTO daily_metrics (date, steps, sleep_hours, resting_heart_rate) VALUES (?, ?, ?, ?)",
        ("2026-01-01", 5000, 7.0, 60),
    )
    conn.commit()

    ensure_schema(conn)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(daily_metrics)")}
    assert set(ADDED_COLUMNS).issubset(columns)
    # Pre-existing row survives the migration, with new columns defaulting to NULL.
    row = conn.execute("SELECT steps, weight_kg FROM daily_metrics WHERE date = '2026-01-01'").fetchone()
    assert row == (5000, None)


def test_ensure_schema_sets_user_version_to_latest():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_ensure_schema_only_runs_each_migration_once():
    """A second call shouldn't re-run any migration — mainly a guard
    against a future migration that (unlike the current two) *isn't*
    naturally idempotent, since user_version having already advanced is
    the only thing that would stop it running again.
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
    version stamp, without erroring or duplicating any column.
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        f"""
        CREATE TABLE daily_metrics (
            date TEXT PRIMARY KEY,
            steps INTEGER,
            sleep_hours REAL,
            resting_heart_rate INTEGER,
            {", ".join(f"{name} {sqltype}" for name, sqltype in ADDED_COLUMNS.items())}
        );
        """
    )
    conn.commit()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0

    ensure_schema(conn)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    columns = {row[1] for row in conn.execute("PRAGMA table_info(daily_metrics)")}
    assert set(ADDED_COLUMNS).issubset(columns)


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


def test_upsert_metrics_inserts_new_row():
    conn = _conn_with_schema()
    upsert_metrics(conn, [{"date": "2026-08-01", "steps": 8000, "mood": 4}])
    row = conn.execute("SELECT steps, mood, weight_kg FROM daily_metrics WHERE date = '2026-08-01'").fetchone()
    assert row == (8000, 4, None)


def test_upsert_metrics_leaves_unmentioned_columns_untouched():
    conn = _conn_with_schema()
    upsert_metrics(conn, [{"date": "2026-08-01", "steps": 8000, "mood": 4}])
    # A second upsert for the same date, only setting a different column.
    upsert_metrics(conn, [{"date": "2026-08-01", "weight_kg": 70.5}])
    row = conn.execute("SELECT steps, mood, weight_kg FROM daily_metrics WHERE date = '2026-08-01'").fetchone()
    assert row == (8000, 4, 70.5)


def test_upsert_metrics_handles_rows_with_different_column_sets_in_one_call():
    conn = _conn_with_schema()
    upsert_metrics(
        conn,
        [
            {"date": "2026-08-01", "steps": 8000},
            {"date": "2026-08-02", "mood": 3, "water_ml": 2000},
        ],
    )
    rows = {
        r[0]: r[1:]
        for r in conn.execute("SELECT date, steps, mood, water_ml FROM daily_metrics ORDER BY date")
    }
    assert rows["2026-08-01"] == (8000, None, None)
    assert rows["2026-08-02"] == (None, 3, 2000)


def test_upsert_metrics_rejects_row_without_date():
    conn = _conn_with_schema()
    with pytest.raises(ValueError):
        upsert_metrics(conn, [{"steps": 1000}])


def test_validate_metrics_accepts_in_range_values():
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
        upsert_metrics(conn, [{"date": "2026-01-01", "steps": 5000}])
    finally:
        conn.close()

    with readonly_connection(db_path) as conn:
        conn.row_factory = row_class()
        row = conn.execute("SELECT steps FROM daily_metrics WHERE date = ?", ("2026-01-01",)).fetchone()
        assert dict(row)["steps"] == 5000


def test_readonly_connection_actually_blocks_writes(tmp_path):
    db_path = tmp_path / "test.db"
    conn = connect_writable(db_path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()

    with readonly_connection(db_path) as conn, pytest.raises(sqlite3.Error):
        conn.execute("INSERT INTO daily_metrics (date, steps) VALUES ('2026-01-01', 1)")


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
        upsert_metrics(conn, [{"date": "2026-01-01", "steps": 4200}])
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
        upsert_metrics(conn, [{"date": "2026-01-01", "steps": 4200}])
    finally:
        conn.close()

    with readonly_connection(db_path) as conn:
        conn.row_factory = row_class()
        row = conn.execute("SELECT steps FROM daily_metrics WHERE date = ?", ("2026-01-01",)).fetchone()
        assert dict(row)["steps"] == 4200


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
        upsert_metrics(conn, [{"date": "2026-01-01", "steps": 1}])
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
            conn.execute("INSERT INTO daily_metrics (date, steps) VALUES (?, ?)", ("2026-01-01", 1))
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
        upsert_metrics(second_conn, [{"date": "2026-01-02", "steps": 2}])
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
    rows = {r[0]: r[1] for r in check_conn.execute("SELECT date, steps FROM daily_metrics")}
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
            upsert_metrics(conn, [{"date": f"2026-02-{i + 1:02d}", "steps": i * 100}])
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
