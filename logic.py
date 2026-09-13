"""
logic.py
========
Framework-free helpers shared by server.py and init_db.py: date parsing,
date-range resolution, numeric aggregation, and the health database schema.

Deliberately dependency-free (standard library only) so it can be imported
and unit-tested without installing fastmcp — see tests/test_logic.py. The
one exception is optional, at-rest database encryption support: if
HEALTH_DB_PASSPHRASE is set, connect_writable/readonly_connection open the
database through the sqlcipher3 package instead of the standard library's
sqlite3, so the file itself is unreadable without that passphrase — see
"Database encryption" below, and README.md for setup and the (considerable)
alternative of just using OS-level full-disk encryption instead.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger("quantified-self-mcp")

HEALTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_metrics (
    date TEXT PRIMARY KEY,
    steps INTEGER,
    sleep_hours REAL,
    resting_heart_rate INTEGER
);
"""

# Columns added after the original release. Kept separate from HEALTH_SCHEMA
# (rather than just relying on CREATE TABLE) because CREATE TABLE IF NOT
# EXISTS does nothing for a daily_metrics table that already exists from an
# older version of this project — the v2 migration below adds these to any
# such table so upgrading never requires deleting your database.
ADDED_COLUMNS = {
    "weight_kg": "REAL",
    "workout_minutes": "INTEGER",
    "mood": "INTEGER",
    "water_ml": "INTEGER",
}

# Raw, source-level measurements — one row per observation rather than one
# row per day. daily_metrics stays the aggregate table analytics.py reads;
# measurements is the finer-grained layer underneath it: multiple
# measurements of the same metric on the same day (e.g. several workouts,
# or an Apple Watch reading heart rate every few minutes) can each be kept,
# with enough context (timestamp, source, source_type) to later explain a
# daily_metrics value rather than just state it. Nothing currently derives
# daily_metrics rows from this table automatically — see
# aggregate_measurements_to_daily for a helper that does that on request.
# Provenance columns added after measurements' original release (schema
# v3 -> v4) — *where* an observation came from, distinct from `source`
# (the device/app itself): `importer` is which import path wrote the row
# ("apple-health", "csv", or None for a manually logged measurement),
# `imported_at` is when that import ran, separate from `timestamp` (when
# the observation itself happened) and `created_at` (when this row was
# inserted, i.e. import time as well but not intended to be app-facing).
# Kept as a v4 ALTER TABLE (rather than folding into MEASUREMENTS_SCHEMA's
# CREATE TABLE) for the same reason ADDED_COLUMNS/v2 is separate from
# HEALTH_SCHEMA: CREATE TABLE IF NOT EXISTS is a no-op against a
# measurements table that already exists from schema v3.
MEASUREMENT_PROVENANCE_COLUMNS = {
    "importer": "TEXT",
    "imported_at": "TEXT",
}

MEASUREMENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT,
    source TEXT,
    source_type TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_measurements_metric_timestamp ON measurements (metric, timestamp);
"""


def _migrate_v1_create_table(conn: sqlite3.Connection) -> None:
    conn.executescript(HEALTH_SCHEMA)


def _migrate_v2_add_weight_workout_mood_water(conn: sqlite3.Connection) -> None:
    # Guarded by an existence check (rather than a bare ALTER TABLE) so this
    # stays safe to run against a database that already has some or all of
    # these columns from before schema versioning existed — see
    # test_ensure_schema_heals_version_for_a_pre_versioning_database.
    existing = {row[1] for row in conn.execute("PRAGMA table_info(daily_metrics)")}
    for name, sqltype in ADDED_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE daily_metrics ADD COLUMN {name} {sqltype}")


# Ordered, versioned migrations, applied via SQLite's own PRAGMA user_version
# (an integer stored in the database file itself — no separate migrations
# table needed). Add new schema changes by appending a new (version,
# description, function) entry here; never edit or remove an existing one,
# even to fix a mistake, since a migration may have already run against a
# real database — write a new migration to correct it instead.
def _migrate_v3_create_measurements_table(conn: sqlite3.Connection) -> None:
    conn.executescript(MEASUREMENTS_SCHEMA)


def _migrate_v4_add_measurement_provenance_columns(conn: sqlite3.Connection) -> None:
    # Guarded existence check, same reasoning as v2 above: safe to re-run
    # against a measurements table that already has these columns.
    existing = {row[1] for row in conn.execute("PRAGMA table_info(measurements)")}
    for name, sqltype in MEASUREMENT_PROVENANCE_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE measurements ADD COLUMN {name} {sqltype}")


V5_ADDED_COLUMNS = {
    "heart_rate": "INTEGER",
    "hrv_ms": "REAL",
}


def _migrate_v5_add_heart_rate_hrv_columns(conn: sqlite3.Connection) -> None:
    # Same guarded-existence pattern as v2: safe against a daily_metrics
    # table that already has one or both columns.
    existing = {row[1] for row in conn.execute("PRAGMA table_info(daily_metrics)")}
    for name, sqltype in V5_ADDED_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE daily_metrics ADD COLUMN {name} {sqltype}")


MIGRATIONS: list[tuple[int, str, Callable[[sqlite3.Connection], None]]] = [
    (1, "create daily_metrics table", _migrate_v1_create_table),
    (2, "add weight_kg, workout_minutes, mood, water_ml columns", _migrate_v2_add_weight_workout_mood_water),
    (3, "create measurements table", _migrate_v3_create_measurements_table),
    (4, "add importer, imported_at provenance columns to measurements", _migrate_v4_add_measurement_provenance_columns),
    (5, "add heart_rate, hrv_ms columns", _migrate_v5_add_heart_rate_hrv_columns),
]

SCHEMA_VERSION = MIGRATIONS[-1][0]

# Guardrails for read_health_data (server.py).
MAX_RANGE_DAYS = 3660  # ~10 years — a wider request is almost certainly a mistake
MAX_ROWS_RETURNED = 400  # ~13 months of daily rows; "summary" still covers the full range


# How long a writer waits for a lock before giving up, rather than
# failing immediately with "database is locked". Long enough to ride out
# a concurrent writer's transaction (log_daily_metric/clear_metric calls
# are single-row and fast), short enough that a genuinely stuck lock
# still surfaces quickly instead of hanging a tool call.
BUSY_TIMEOUT_MS = 5000


def _dir_is_writable(path: Path) -> bool:
    """Best-effort check that `path` exists (creating it if needed) and
    that a file can actually be written inside it.

    Used by default_data_dir below to tell a normal source checkout
    (writable) apart from a system-wide `pip install`, where this
    project's files live inside site-packages and an ordinary user has
    no permission to write there.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path):
            pass
        return True
    except OSError:
        return False


def _user_data_dir() -> Path:
    """A per-user data directory, following each platform's usual
    convention. Used as the fallback default database location when the
    source-checkout convention (a data/ folder next to server.py) isn't
    writable.
    """
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        return Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    return Path(xdg_data_home) if xdg_data_home else Path.home() / ".local" / "share"


def default_data_dir(base_dir: Path) -> Path:
    """Pick a sensible default directory for the health database.

    Prefers `data/` next to base_dir (the documented source-checkout
    layout, and what CI expects). Falls back to a per-user data directory
    — e.g. ~/.local/share/quantified-self-mcp on Linux — when that isn't
    writable, which is the common case for a system-wide `pip install`:
    base_dir then points inside site-packages, which ordinary users can't
    write to. Either way, HEALTH_DB_PATH or --db-path still override this
    entirely.
    """
    source_checkout_dir = base_dir / "data"
    if _dir_is_writable(source_checkout_dir):
        return source_checkout_dir
    return _user_data_dir() / "quantified-self-mcp"


# ---------------------------------------------------------------------------
# Database encryption (optional)
# ---------------------------------------------------------------------------
#
# Plain SQLite (the default, and everything above this point) stores
# health.db as an ordinary, unencrypted file — readable by anything with
# filesystem access to it, same as any other file on disk. See README.md
# for why OS-level full-disk encryption (FileVault, BitLocker, LUKS) is
# the recommended baseline regardless, and is enough for most people on
# a single-user machine.
#
# For the additional case of an at-rest-encrypted *database file itself*
# (e.g. the file might be synced to a cloud drive, or the machine is
# shared), set HEALTH_DB_PASSPHRASE and install the optional sqlcipher3
# package (`pip install sqlcipher3-binary`, or `pip install
# quantified-self-mcp[encryption]`). Everything below is a no-op — same
# plain sqlite3 as always — unless that env var is set.

DB_PASSPHRASE_ENV = "HEALTH_DB_PASSPHRASE"

try:
    import sqlcipher3 as _sqlcipher
except ImportError:
    _sqlcipher = None


def encryption_available() -> bool:
    """Whether the optional sqlcipher3 package is installed — i.e.
    whether HEALTH_DB_PASSPHRASE can actually be used right now. Exposed
    for init_db.py/server.py to give a clear, specific error message up
    front rather than an obscure one from wherever the first query happens
    to run.
    """
    return _sqlcipher is not None


def _db_passphrase() -> str | None:
    return os.environ.get(DB_PASSPHRASE_ENV) or None


def _db_module():
    """Which DB-API module every connection in this file goes through:
    the standard library's sqlite3 (the default, unencrypted), or
    sqlcipher3 if HEALTH_DB_PASSPHRASE is set. Raises with a specific,
    actionable message if a passphrase is configured but sqlcipher3 isn't
    installed — silently falling back to a plaintext connection instead
    would be a dangerous way to fail for a security setting.
    """
    if _db_passphrase() is None:
        return sqlite3
    if _sqlcipher is None:
        raise RuntimeError(
            f"{DB_PASSPHRASE_ENV} is set, but the sqlcipher3 package needed to open an "
            "encrypted database isn't installed. Install it with `pip install "
            "sqlcipher3-binary` (or `pip install quantified-self-mcp[encryption]`), or "
            f"unset {DB_PASSPHRASE_ENV} to use a plain, unencrypted database instead."
        )
    return _sqlcipher


def row_class() -> type:
    """The Row class matching whichever DB-API module is currently active
    (see _db_module) — sqlite3.Row for a plain database, sqlcipher3's own
    Row for an encrypted one. The two aren't interchangeable: sqlite3.Row
    rejects a sqlcipher3 cursor outright (a C-level type check), so any
    code setting `conn.row_factory` on a connection from this module must
    use row_class() rather than hardcoding sqlite3.Row.
    """
    return _db_module().Row


def db_error_types() -> tuple[type[Exception], ...]:
    """Exception classes to catch for "something went wrong talking to
    the database", matching whichever module is currently active (see
    _db_module). sqlite3 and sqlcipher3 don't share an exception
    hierarchy — sqlcipher3.dbapi2.Error is not a sqlite3.Error subclass —
    so code that only ever caught sqlite3.Error would let a database
    problem escape as an unhandled exception whenever encryption is on.
    Always includes sqlite3.Error even when encrypted, since a handful of
    error paths (e.g. a plain sqlite3.Error raised directly by this
    module's own code, not the driver) can still occur either way.
    """
    module = _db_module()
    return (sqlite3.Error,) if module is sqlite3 else (sqlite3.Error, module.Error)


def _escape_pragma_string(value: str) -> str:
    # SQLite/SQLCipher PRAGMA statements don't accept bound (?) parameters
    # — the value has to be embedded directly into the SQL text. Escaping
    # this the same way a SQL string literal would be (doubling any single
    # quote) keeps a passphrase containing a quote from breaking out of
    # the literal, the same concern parameterization would normally cover.
    return value.replace("'", "''")


def _apply_encryption_key(conn: sqlite3.Connection, module, db_path: Any) -> None:
    """If HEALTH_DB_PASSPHRASE is set, key `conn` with it and immediately
    verify the key actually works (a wrong passphrase, or opening a
    plaintext file as if it were encrypted, doesn't fail until the first
    real read otherwise — surfacing that here, in the shared connection
    path, gives one clear error instead of a confusing failure wherever
    the first query happens to be).
    """
    passphrase = _db_passphrase()
    if passphrase is None:
        return
    conn.execute(f"PRAGMA key = '{_escape_pragma_string(passphrase)}'")
    try:
        conn.execute("SELECT count(*) FROM sqlite_master")
    except module.DatabaseError as exc:
        conn.close()
        raise RuntimeError(
            f"Could not open the encrypted database at {db_path}: wrong "
            f"{DB_PASSPHRASE_ENV}, or this file isn't a SQLCipher database ({exc})"
        ) from exc


def connect_writable(db_path: Any) -> sqlite3.Connection:
    """Open db_path for writing, with a busy_timeout set so a momentary
    lock from a concurrent writer causes a short wait instead of an
    immediate error. Used by every writer: init_db.py, log_daily_metric,
    clear_metric, and the migration step in server.py's _ensure_db.

    Transparently encrypted via SQLCipher if HEALTH_DB_PASSPHRASE is set
    — see "Database encryption" above this function.
    """
    module = _db_module()
    conn = module.connect(str(db_path))
    _apply_encryption_key(conn, module, db_path)
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


@contextmanager
def readonly_connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open db_path read-only so the caller cannot write to it.

    Prefers SQLite's URI mode=ro, which enforces this at the driver level.
    Falls back to a normal connection guarded by PRAGMA query_only if
    mode=ro fails to open the file — which happens if a previous write left
    a WAL/journal file pending recovery, something SQLite refuses to do
    while read-only. The fallback still blocks writes, just via SQL rather
    than the OS-level open flag. Either way, busy_timeout is set so a read
    landing at the exact instant a write commits waits briefly rather than
    failing immediately (WAL mode, enabled in ensure_schema, makes this
    rare in the first place — readers don't normally block on a writer at
    all). row_factory is set to row_class() (see there for why this
    matters when the database is encrypted).

    Moved here from server.py (which now just re-exports this) since none
    of this is actually MCP-specific — it's the same database-opening
    decision connect_writable makes, just read-only, and both need to
    agree on which driver module is in play for a given HEALTH_DB_PASSPHRASE.
    """
    db_path = Path(db_path)
    module = _db_module()
    uri = db_path.resolve().as_uri() + "?mode=ro"
    try:
        conn = module.connect(uri, uri=True)
        _apply_encryption_key(conn, module, db_path)
        conn.execute("SELECT 1")  # force the open now, not on the caller's first real query
    except module.OperationalError:
        logger.warning(
            "Could not open %s read-only (likely a pending WAL/journal); "
            "falling back to a query_only connection.",
            db_path,
        )
        conn = module.connect(str(db_path))
        _apply_encryption_key(conn, module, db_path)
        conn.execute("PRAGMA query_only = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.row_factory = row_class()
    try:
        yield conn
    finally:
        conn.close()


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Bring `conn`'s schema up to SCHEMA_VERSION, running whichever
    migrations in MIGRATIONS haven't been applied yet.

    Tracked via SQLite's own `PRAGMA user_version` — an integer stored in
    the database file itself, so no separate migrations table is needed.
    A brand-new database starts at 0, so every migration runs. A database
    from before this versioning existed is also at 0 despite already
    having some (or all) of these columns; each migration function is
    itself idempotent specifically so re-running one against such a
    database is always safe — see
    test_ensure_schema_heals_version_for_a_pre_versioning_database.

    A database with a *higher* version than this code knows about (opened
    with an older release, after being upgraded by a newer one) is left
    untouched rather than guessed at — the loop below only ever applies
    migrations numbered above the current version, so this is naturally a
    no-op, but a warning is logged since it likely means an upgrade of
    this project itself is needed.

    Also switches the database to WAL journal mode (a no-op if it's a
    real file already in WAL mode, silently ignored for :memory: databases
    in tests). WAL lets read_health_data's readers proceed without
    blocking on a concurrent log_daily_metric/clear_metric write, and vice
    versa — the two only conflict if two writes land at the exact same
    instant, which busy_timeout above then covers.

    Safe and cheap to call on every startup/import — every migration is a
    no-op once already applied, and user_version already at SCHEMA_VERSION
    is the common case. Requires a writable connection; commits before
    returning.
    """
    conn.execute("PRAGMA journal_mode = WAL")
    current_version = conn.execute("PRAGMA user_version").fetchone()[0]
    if current_version > SCHEMA_VERSION:
        logger.warning(
            "Database schema version %d is newer than this version of quantified-self-mcp expects (%d); "
            "leaving it as-is. You may need to upgrade quantified-self-mcp.",
            current_version,
            SCHEMA_VERSION,
        )
        return
    for version, _description, migrate in MIGRATIONS:
        if version <= current_version:
            continue
        migrate(conn)
        # Not parameterized: PRAGMA doesn't accept bound parameters, and
        # `version` is always one of our own MIGRATIONS entries, never
        # user input.
        conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()


def upsert_metrics(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    """Upsert one or more daily_metrics rows by date.

    Each row dict must include "date" plus any subset of the metric
    columns. A column a row doesn't include is left untouched for that
    date rather than cleared — e.g. upserting only {"date": ..., "mood":
    4} never blanks out that day's steps. Rows are grouped by their exact
    set of columns before executemany-ing each group, since a single
    INSERT needs a fixed column list. Requires a writable connection;
    commits before returning. Shared by init_db.py (batch import from a
    CSV) and server.py's log_daily_metric tool (a single row at a time).
    """
    if not rows:
        return
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for row in rows:
        if "date" not in row:
            raise ValueError("Each row passed to upsert_metrics must include 'date'.")
        groups.setdefault(tuple(sorted(row)), []).append(row)

    for columns_key, group_rows in groups.items():
        columns = list(columns_key)
        insert_cols = ", ".join(columns)
        placeholders = ", ".join(f":{c}" for c in columns)
        update_clause = ", ".join(f"{c} = excluded.{c}" for c in columns if c != "date")
        sql = f"INSERT INTO daily_metrics ({insert_cols}) VALUES ({placeholders})"
        sql += f" ON CONFLICT(date) DO UPDATE SET {update_clause}" if update_clause else " ON CONFLICT(date) DO NOTHING"
        conn.executemany(sql, group_rows)
    conn.commit()


def insert_measurement(
    conn: sqlite3.Connection,
    timestamp: str,
    metric: str,
    value: float,
    unit: str | None = None,
    source: str | None = None,
    source_type: str | None = None,
    importer: str | None = None,
    imported_at: str | None = None,
) -> int:
    """Insert one raw measurement row and return its id.

    Unlike upsert_metrics, this always inserts a new row rather than
    upserting by date — a day can have many measurements of the same
    metric (multiple workouts, repeated heart-rate readings, etc.).
    importer/imported_at record provenance for rows written by an
    automated import (see import_adapters.py) — leave both None for a
    measurement logged directly (e.g. via the log_measurement tool).
    Requires a writable connection; commits before returning.
    """
    cursor = conn.execute(
        "INSERT INTO measurements (timestamp, metric, value, unit, source, source_type, importer, imported_at) "
        "VALUES (:timestamp, :metric, :value, :unit, :source, :source_type, :importer, :imported_at)",
        {
            "timestamp": timestamp,
            "metric": metric,
            "value": value,
            "unit": unit,
            "source": source,
            "source_type": source_type,
            "importer": importer,
            "imported_at": imported_at,
        },
    )
    conn.commit()
    return cursor.lastrowid


def query_measurements(
    conn: sqlite3.Connection,
    metric: str | None = None,
    start: str | None = None,
    end: str | None = None,
    source: str | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """Return raw measurement rows matching the given filters, most recent
    first. start/end compare against `timestamp` lexicographically, so
    both plain dates (YYYY-MM-DD) and full ISO timestamps work. All
    filters are optional; omitting them all returns the most recent
    `limit` measurements across every metric.
    """
    clauses, params = [], {}
    if metric is not None:
        clauses.append("metric = :metric")
        params["metric"] = metric
    if start is not None:
        clauses.append("timestamp >= :start")
        params["start"] = start
    if end is not None:
        clauses.append("timestamp <= :end")
        params["end"] = end
    if source is not None:
        clauses.append("source = :source")
        params["source"] = source
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params["limit"] = limit
    cursor = conn.cursor()
    cursor.row_factory = row_class()
    rows = cursor.execute(
        f"SELECT id, timestamp, metric, value, unit, source, source_type, importer, imported_at, created_at "
        f"FROM measurements {where} ORDER BY timestamp DESC LIMIT :limit",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


# Which daily_metrics column each measurements.metric name rolls up into,
# and how same-day values combine — "sum" for cumulative-through-the-day
# metrics (steps, water), "avg" for point-in-time readings (heart rate,
# weight), "last" for whichever was recorded latest. Deliberately a small,
# explicit map rather than assuming metric name == column name, since a
# measurement's metric label (e.g. "resting_heart_rate" from one source,
# "restingHeartRate" from another) isn't guaranteed to match daily_metrics'
# column naming without normalization happening somewhere.
MEASUREMENT_AGGREGATION = {
    "steps": "sum",
    "sleep_hours": "sum",
    "resting_heart_rate": "avg",
    "weight_kg": "last",
    "workout_minutes": "sum",
    "mood": "avg",
    "water_ml": "sum",
    "heart_rate": "avg",
    "hrv_ms": "avg",
}


def get_metric_provenance(conn: sqlite3.Connection, metric: str, day: str) -> dict[str, Any]:
    """Break one metric's measurements for one day down by source, so a
    caller can see e.g. "Apple Watch says 62, Garmin says 67" instead of
    a single blended number with no way to tell they disagreed.

    Returns {"metric", "date", "sources": [{"source", "value" (avg across
    that source's readings that day), "n", "latest_timestamp"}, ...]
    sorted by n descending, "conflict": bool}. "conflict" is True only
    when 2+ distinct non-null sources are present *and* their per-source
    averages differ by more than CONFLICT_TOLERANCE_PCT of the smaller
    one — a single source reporting multiple similar readings is not a
    conflict. Rows with no source recorded are grouped under None.
    """
    cursor = conn.cursor()
    cursor.row_factory = row_class()
    rows = cursor.execute(
        "SELECT source, value, timestamp FROM measurements "
        "WHERE metric = :metric AND timestamp >= :start AND timestamp < :end",
        {"metric": metric, "start": day, "end": day + "T24:00:00"},
    ).fetchall()

    by_source: dict[str | None, list[tuple[float, str]]] = {}
    for row in rows:
        by_source.setdefault(row["source"], []).append((row["value"], row["timestamp"]))

    sources = [
        {
            "source": source,
            "value": round(sum(v for v, _ts in readings) / len(readings), 2),
            "n": len(readings),
            "latest_timestamp": max(readings, key=lambda pair: pair[1])[1],
        }
        for source, readings in by_source.items()
    ]
    sources.sort(key=lambda s: s["n"], reverse=True)

    conflict = False
    distinct_values = [s["value"] for s in sources if s["source"] is not None]
    if len(distinct_values) > 1:
        lo, hi = min(distinct_values), max(distinct_values)
        conflict = lo == 0 or (hi - lo) / lo > CONFLICT_TOLERANCE_PCT

    return {"metric": metric, "date": day, "sources": sources, "conflict": conflict}


# How far apart two sources' same-day averages for a metric can be before
# get_metric_provenance/resolve_source_conflicts calls it a conflict
# rather than ordinary reading-to-reading noise.
CONFLICT_TOLERANCE_PCT = 0.05


def resolve_source_conflicts(
    rows: list[dict[str, Any]], source_priority: list[str] | None = None
) -> tuple[list[dict[str, Any]], bool]:
    """Given measurement rows (as from query_measurements) for a single
    metric/day, decide which to keep when more than one source is present.

    With source_priority given, keeps only the rows from the
    highest-priority source that actually appears (first match in the
    list wins) — e.g. ["Apple Watch", "Garmin"] prefers Apple Watch data
    whenever both are present for that day. Without a priority list,
    falls back to whichever source has the most recent imported_at (or
    created_at if imported_at is null) — i.e. "trust the most recently
    imported source". A single source (or no rows) is returned unchanged.
    Returns (kept_rows, conflict) — conflict is True whenever this
    function actually had to choose between 2+ distinct sources.
    """
    sources = {r.get("source") for r in rows}
    if len(sources) <= 1:
        return rows, False

    if source_priority:
        for preferred in source_priority:
            if preferred in sources:
                return [r for r in rows if r.get("source") == preferred], True

    def _recency_key(row: dict[str, Any]) -> str:
        return row.get("imported_at") or row.get("created_at") or ""

    newest_source = max(rows, key=_recency_key).get("source")
    return [r for r in rows if r.get("source") == newest_source], True


def aggregate_measurements_to_daily(
    conn: sqlite3.Connection, day: str, source_priority: list[str] | None = None
) -> dict[str, Any]:
    """Roll up one day's measurements into a daily_metrics-shaped dict
    (date + whichever metrics have measurements that day), using
    MEASUREMENT_AGGREGATION to decide how same-day values combine. Does
    not write anything itself — pass the result to upsert_metrics to
    actually update daily_metrics. Metrics with no rows for `day`, or no
    entry in MEASUREMENT_AGGREGATION, are omitted rather than written as
    null, matching upsert_metrics' "only touch what's provided" contract.

    When a metric has measurements from more than one source that day,
    resolve_source_conflicts picks which to keep (using source_priority
    if given) before aggregating, rather than blending readings from
    different devices into one number. See get_metric_provenance to
    inspect a disagreement before deciding on a priority.
    """
    cursor = conn.cursor()
    cursor.row_factory = row_class()
    rows = [
        dict(r)
        for r in cursor.execute(
            "SELECT metric, value, timestamp, source, imported_at, created_at FROM measurements "
            "WHERE timestamp >= :start AND timestamp < :end",
            {"start": day, "end": day + "T24:00:00"},
        ).fetchall()
    ]
    by_metric: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_metric.setdefault(row["metric"], []).append(row)

    result: dict[str, Any] = {"date": day}
    for metric, metric_rows in by_metric.items():
        how = MEASUREMENT_AGGREGATION.get(metric)
        if how is None:
            continue
        kept_rows, _conflict = resolve_source_conflicts(metric_rows, source_priority)
        values = [(r["value"], r["timestamp"]) for r in kept_rows]
        if how == "sum":
            result[metric] = sum(v for v, _ts in values)
        elif how == "avg":
            result[metric] = round(sum(v for v, _ts in values) / len(values), 1)
        elif how == "last":
            result[metric] = max(values, key=lambda pair: pair[1])[0]
    return result


# Sanity bounds for each metric: (min, max, human label used in error messages).
# Deliberately generous — meant to catch obvious mistakes (unit confusion,
# a slipped decimal point, a fat-fingered extra digit) rather than to police
# what's "normal". mood is fixed at 1-10 so the scale is consistent across
# every log_daily_metric call, rather than left to whatever scale a given
# session happens to use.
METRIC_BOUNDS = {
    "steps": (0, 200_000, "steps"),
    "sleep_hours": (0, 24, "sleep_hours"),
    "resting_heart_rate": (20, 250, "resting_heart_rate (bpm)"),
    "weight_kg": (1, 500, "weight_kg"),
    "workout_minutes": (0, 1440, "workout_minutes"),
    "mood": (1, 10, "mood (expected on a 1-10 scale)"),
    "water_ml": (0, 10_000, "water_ml"),
    "heart_rate": (20, 250, "heart_rate (bpm)"),
    "hrv_ms": (0, 300, "hrv_ms (ms)"),
}


def validate_metrics(metrics: dict[str, Any]) -> None:
    """Raise ValueError if any value in `metrics` (column name -> value,
    typically from a partial log_daily_metric call or a parsed CSV row)
    falls outside METRIC_BOUNDS. None values and unrecognized keys are
    ignored, so this is safe to call on any subset of columns.
    """
    for name, value in metrics.items():
        if value is None or name not in METRIC_BOUNDS:
            continue
        low, high, label = METRIC_BOUNDS[name]
        if not (low <= value <= high):
            raise ValueError(f"{label} must be between {low} and {high}, got {value}")


def parse_date(value: str, field_name: str) -> date:
    """Parse a YYYY-MM-DD string, raising a clear, client-facing ValueError otherwise."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{field_name} must be formatted YYYY-MM-DD, got {value!r}") from exc


def resolve_range(
    start_date: str | None, end_date: str | None, default_days: int
) -> tuple[date, date]:
    """Fill in sensible defaults for an open-ended date range and validate it."""
    end = parse_date(end_date, "end_date") if end_date else date.today()
    start = parse_date(start_date, "start_date") if start_date else end - timedelta(days=default_days)
    if start > end:
        raise ValueError(f"start_date ({start}) is after end_date ({end})")
    span = (end - start).days
    if span > MAX_RANGE_DAYS:
        raise ValueError(
            f"Requested range is {span} days, which is over the {MAX_RANGE_DAYS}-day limit. "
            "Narrow start_date/end_date and try again."
        )
    return start, end


def numeric_stats(rows: list[dict[str, Any]], key: str) -> dict[str, float | None]:
    values = [r[key] for r in rows if r.get(key) is not None]
    if not values:
        return {"avg": None, "min": None, "max": None}
    return {"avg": round(sum(values) / len(values), 1), "min": min(values), "max": max(values)}
