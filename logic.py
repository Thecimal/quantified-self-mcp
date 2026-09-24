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

from db import invariant as db_invariant
from metric_registry import metric_bounds

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


# workout_sessions holds one row per discrete workout, with the structured
# detail a single workout_minutes number can't carry: what kind of activity
# it was, when it started, how intense it was, and the heart-rate response
# to it. This sits alongside daily_metrics/measurements rather than
# replacing either — workout_minutes on daily_metrics stays the fast daily
# total analytics.py reads, while a day's workout_sessions rows are what
# explain_metric_change and get_recent_changes pull in to say *why* that
# total looks the way it does.
WORKOUT_SESSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS workout_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    activity_type TEXT NOT NULL,
    start_time TEXT,
    duration_minutes INTEGER NOT NULL,
    intensity TEXT,
    avg_heart_rate INTEGER,
    max_heart_rate INTEGER,
    source TEXT,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_workout_sessions_date ON workout_sessions (date);
"""

# Free-form activity_type is allowed (see insert_workout_session), but
# intensity is kept to a fixed, small vocabulary so callers building
# on top of it (analytics, explain_metric_change) can compare/group on it
# reliably instead of dealing with arbitrary strings.
WORKOUT_INTENSITIES = frozenset({"low", "moderate", "high"})


def _migrate_v6_create_workout_sessions_table(conn: sqlite3.Connection) -> None:
    conn.executescript(WORKOUT_SESSIONS_SCHEMA)


# Every column daily_metrics has ever gained (v1's original three plus
# ADDED_COLUMNS/V5_ADDED_COLUMNS) -- exactly the metric set db/schema.sql's
# aggregation_rules seeds, so a value in any of these columns is guaranteed
# to have a home in the new projection. Kept as its own tuple (rather than
# reused from METRIC_COLUMNS, which lives in server.py) since this module
# has no server.py dependency and migrations must stay self-contained.
_V6_DAILY_METRICS_COLUMNS = ("steps", "sleep_hours", "resting_heart_rate", *ADDED_COLUMNS, *V5_ADDED_COLUMNS)


def _migrate_v7_project_daily_metrics_from_measurements(conn: sqlite3.Connection) -> None:
    """Retires the old daily_metrics table -- one row per date, one column
    per metric, upserted into directly by the old upsert_metrics -- in
    favor of the narrow, trigger-maintained projection defined in
    db/schema.sql and db/invariant.py: one row per (date, metric), derived
    from measurements and never written to directly. See those modules for
    that design.

    Every value already sitting in the wide table has no measurements row
    backing it (it was written straight into daily_metrics by
    upsert_metrics/log_daily_metric/init_db's CSV import, never through
    insert_measurement), so before the old table is renamed out of the
    way, each non-null cell is turned into one synthetic measurements row
    (importer="legacy-daily-metrics-migration") -- this both preserves the
    value and gives the new projection something to derive it from. A
    synthetic row's timestamp is noon on its date, since the old table
    only ever tracked a day, not a time of day.

    By the time this runs, migrations 1/2/5 guarantee the wide table
    exists with its full historical column set (empty for a brand-new
    database, populated for one that predates this migration) -- so unlike
    v2/v4/v5's defensive existence checks (needed because *those* run
    against a database that might already have some of their columns from
    before schema versioning existed), this one can assume the wide shape
    unconditionally; PRAGMA user_version guarantees it only ever runs
    once, and only after 1/2/5 already have.

    Renames rather than drops the old table (to daily_metrics_legacy_v6)
    so nothing is destroyed if this needs auditing later. db_invariant
    .bootstrap() then creates the new narrow daily_metrics table fresh
    (the rename freed the name), and repair() populates it from
    measurements, migrated rows included.
    """
    columns = ", ".join(_V6_DAILY_METRICS_COLUMNS)
    rows = conn.execute(f"SELECT date, {columns} FROM daily_metrics").fetchall()
    imported_at = datetime.now().isoformat(timespec="seconds")
    synthetic = [
        {
            "timestamp": f"{row[0]}T12:00:00",
            "metric": metric,
            "value": value,
            "unit": None,
            "source": None,
            "source_type": None,
            "importer": "legacy-daily-metrics-migration",
            "imported_at": imported_at,
        }
        for row in rows
        for metric, value in zip(_V6_DAILY_METRICS_COLUMNS, row[1:], strict=True)
        if value is not None
    ]
    if synthetic:
        conn.executemany(
            "INSERT INTO measurements (timestamp, metric, value, unit, source, source_type, importer, imported_at) "
            "VALUES (:timestamp, :metric, :value, :unit, :source, :source_type, :importer, :imported_at)",
            synthetic,
        )
    conn.execute("ALTER TABLE daily_metrics RENAME TO daily_metrics_legacy_v6")
    conn.commit()
    db_invariant.bootstrap(conn)
    db_invariant.repair(conn)


# Columns daily_metrics gained when the projection started resolving one source
# per (metric, day) -- see db/aggregation.py. daily_metrics is derived data, so
# adding them and rebuilding loses nothing.
_V8_DAILY_METRICS_COLUMNS = {
    "resolved_source": "TEXT",
    "source_count": "INTEGER NOT NULL DEFAULT 1",
    "resolution": "TEXT NOT NULL DEFAULT 'single' CHECK (resolution IN ('single', 'priority', 'fallback'))",
}


def _migrate_v8_resolve_sources_in_projection(conn: sqlite3.Connection) -> None:
    """Give daily_metrics its source-resolution columns, create source_priority,
    seed the one default rank (manual daily-log entries first, once, so removing
    it later sticks), and rebuild the projection so days that used to blend or
    sum several sources are recomputed from a single one. Guarded like v2/v4/v5:
    a brand-new database reaches this with the columns already there, because
    v7 creates the table from the current db/schema.sql."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(daily_metrics)")}
    for name, declaration in _V8_DAILY_METRICS_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE daily_metrics ADD COLUMN {name} {declaration}")
    conn.commit()
    db_invariant.bootstrap(conn)
    conn.execute(
        "INSERT OR IGNORE INTO source_priority (metric, rank, source) VALUES ('*', 1, ?)", (DAILY_LOG_SOURCE,)
    )
    conn.commit()
    db_invariant.repair(conn)


MIGRATIONS: list[tuple[int, str, Callable[[sqlite3.Connection], None]]] = [
    (1, "create daily_metrics table", _migrate_v1_create_table),
    (2, "add weight_kg, workout_minutes, mood, water_ml columns", _migrate_v2_add_weight_workout_mood_water),
    (3, "create measurements table", _migrate_v3_create_measurements_table),
    (4, "add importer, imported_at provenance columns to measurements", _migrate_v4_add_measurement_provenance_columns),
    (5, "add heart_rate, hrv_ms columns", _migrate_v5_add_heart_rate_hrv_columns),
    (6, "create workout_sessions table", _migrate_v6_create_workout_sessions_table),
    (
        7,
        "project daily_metrics from measurements (retires the wide per-metric-column table)",
        _migrate_v7_project_daily_metrics_from_measurements,
    ),
    (
        8,
        "resolve one source per (metric, day) in the daily_metrics projection; add source_priority",
        _migrate_v8_resolve_sources_in_projection,
    ),
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

    Also (re)installs the measurements -> daily_metrics trigger set on
    every call, via db_invariant.bootstrap() — cheap and idempotent for
    the same reason the rest of this function is (see db/invariant.py),
    and needed on every startup, not just the one-time v7 migration that
    first creates the narrow daily_metrics table: bootstrap() is what
    keeps the triggers installed, migrations only touch table DDL.

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
    db_invariant.bootstrap(conn)


# Sentinel `measurements.source` value for a row written by
# upsert_daily_metric_measurements — i.e. a direct "this is the day's
# total" declaration (log_daily_metric), as opposed to a granular
# log_measurement reading or an importer-tagged row (measurements.importer
# is set instead, for those). Lets get_metric_provenance/clear_daily_metric
# tell a manually-declared daily total apart from everything else backing
# a given day's aggregated value.
DAILY_LOG_SOURCE = "daily-log"


def upsert_daily_metric_measurements(conn: sqlite3.Connection, day: str, metrics: dict[str, Any]) -> None:
    """Write one or more metrics as `day`'s manually-declared total,
    replacing any prior daily-log value for the same (metric, day) —
    the measurements-layer equivalent of the old upsert_metrics' upsert-
    by-date behavior, now that daily_metrics is a trigger-maintained
    projection over measurements rather than a table written to
    directly (see db/schema.sql, db/invariant.py).

    Each metric is tagged source=DAILY_LOG_SOURCE, distinguishing it from
    an ad hoc log_measurement reading (which should accumulate, not
    overwrite) or an importer's rows (which carry `importer` instead) —
    see clear_daily_metric, which removes it (and anything else backing
    that day's value for that metric). Metrics not present in `metrics`
    are left untouched, matching the old "leaves unmentioned columns
    untouched" contract. Requires a writable connection; commits before
    returning. Used by server.py's log_daily_metric tool.
    """
    if not metrics:
        return
    timestamp = f"{day}T12:00:00"
    for metric, value in metrics.items():
        conn.execute(
            "DELETE FROM measurements WHERE metric = :metric AND date(timestamp) = :day AND source = :source",
            {"metric": metric, "day": day, "source": DAILY_LOG_SOURCE},
        )
        conn.execute(
            "INSERT INTO measurements (timestamp, metric, value, source) VALUES (:timestamp, :metric, :value, :source)",
            {"timestamp": timestamp, "metric": metric, "value": value, "source": DAILY_LOG_SOURCE},
        )
    conn.commit()


def clear_daily_metric(conn: sqlite3.Connection, day: str, metric: str) -> int:
    """Delete every measurements row for (metric, day) — i.e. every
    observation behind that day's current daily_metrics value for that
    metric, not just one written by upsert_daily_metric_measurements.
    The AFTER DELETE trigger then removes the daily_metrics projection
    row for that key once no measurements remain (see db/schema.sql) —
    nothing here touches daily_metrics directly.

    This is intentionally broader than the old `UPDATE daily_metrics SET
    <field> = NULL`: if granular log_measurement readings or imported
    rows also exist for that day/metric, they're cleared too, so the
    metric genuinely goes back to "nothing recorded" rather than falling
    back to a blended value from whatever's left. Requires a writable
    connection; commits before returning. Returns the number of
    measurement rows deleted (0 if there was nothing to clear). Used by
    server.py's clear_metric tool.
    """
    cursor = conn.execute(
        "DELETE FROM measurements WHERE metric = :metric AND date(timestamp) = :day",
        {"metric": metric, "day": day},
    )
    conn.commit()
    return cursor.rowcount


def daily_metrics_wide(
    conn: sqlite3.Connection, metrics: list[str], start: str | None = None, end: str | None = None
) -> list[dict[str, Any]]:
    """Pivot the narrow (date, metric, value) daily_metrics projection
    (see db/schema.sql) into the one-row-per-date, one-column-per-metric
    shape the table itself had before the measurements/daily_metrics
    invariant work — what server.py's tools return.

    `metrics` fixes both which columns come back and their order; a date
    with no value for a given requested metric gets NULL for that
    column, same as before. A date with no row for *any* requested
    metric is simply absent from the result (matching the old "only
    dates with at least one recorded metric appear" contract) — pass
    start == end for a single date's row (absent from the result if that
    date has none of the requested metrics). start/end (inclusive,
    YYYY-MM-DD) optionally bound the date range; omit both for every
    date that has at least one of the requested metrics, unbounded.
    Read-only.
    """
    if not metrics:
        raise ValueError("daily_metrics_wide requires at least one metric")
    params: dict[str, Any] = {f"m{i}": metric for i, metric in enumerate(metrics)}
    pivot_cols = ",\n        ".join(
        f"MAX(CASE WHEN metric = :m{i} THEN value END) AS {metric}" for i, metric in enumerate(metrics)
    )
    clauses = ["metric IN (" + ", ".join(f":m{i}" for i in range(len(metrics))) + ")"]
    if start is not None:
        clauses.append("date >= :start")
        params["start"] = start
    if end is not None:
        clauses.append("date <= :end")
        params["end"] = end
    where = " AND ".join(clauses)
    cursor = conn.cursor()
    cursor.row_factory = row_class()
    rows = cursor.execute(
        f"SELECT date,\n        {pivot_cols}\n        FROM daily_metrics WHERE {where} GROUP BY date ORDER BY date",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def measurement_rows_from_daily(
    rows: list[dict[str, Any]], skip_metrics: frozenset[str] = frozenset()
) -> list[dict[str, Any]]:
    """Flatten init_db.py's per-day wide rows (one dict per date, one key
    per metric column — e.g. {"date": "2026-01-01", "steps": 8000}) into
    one measurements-table row per (date, metric): {"timestamp":
    "2026-01-01T12:00:00", "metric": "steps", "value": 8000}, the shape
    bulk_import_measurements/db_invariant.bulk_insert_measurements expect.

    A day with no value for a metric contributes no row for it, matching
    the old upsert_metrics behavior of leaving that column untouched
    rather than writing a spurious zero/NULL. `skip_metrics` excludes
    columns that already have granular per-event data elsewhere in the
    same import (e.g. an adapter's raw_measurements) — including both
    would double-count a "sum" metric under the new trigger-derived
    aggregation, since the wide row would otherwise contribute its own
    pre-aggregated total *on top of* the real per-event readings. Each
    row's timestamp is noon on its date, matching the "only a day is
    known, not a time" placeholder used elsewhere (see
    _migrate_v7_project_daily_metrics_from_measurements).
    """
    return [
        {"timestamp": f"{row['date']}T12:00:00", "metric": metric, "value": value}
        for row in rows
        for metric, value in row.items()
        if metric != "date" and value is not None and metric not in skip_metrics
    ]


def bulk_import_measurements(
    conn: sqlite3.Connection, importer: str, rows: list[dict[str, Any]], replace: bool = False
) -> dict:
    """Load `rows` (each needing metric/value/timestamp; unit/source/
    source_type optional) into measurements tagged importer=`importer`,
    then rebuild the daily_metrics projection over them, via
    db_invariant.bulk_insert_measurements — the shared write path for
    init_db.py's CSV/adapter imports (both the day-total rows
    measurement_rows_from_daily produces and an adapter's own
    raw_measurements).

    Re-running the same import stays idempotent by default, matching
    init_db.py's documented "safe to re-run" contract: any existing row
    for a (metric, date) this batch also touches is deleted first, so
    reloading an unchanged source file doesn't double-count into a "sum"
    metric. replace=True goes further — wiping *every* row this importer
    has ever written (not just dates present in this run) before
    loading, for a source file that has since dropped some dates. Either
    way, only rows tagged with this importer are ever touched — manually
    logged measurements (log_measurement, upsert_daily_metric_measurements)
    and other importers' rows are untouched. Requires a writable
    connection. Returns the db_invariant.verify() result after rebuilding.
    """
    imported_at = datetime.now().isoformat(timespec="seconds")
    tagged = [{**row, "importer": importer, "imported_at": row.get("imported_at", imported_at)} for row in rows]

    if replace:
        conn.execute("DELETE FROM measurements WHERE importer = :importer", {"importer": importer})
    elif tagged:
        touched = {(row["metric"], row["timestamp"][:10]) for row in tagged}
        conn.executemany(
            "DELETE FROM measurements WHERE importer = :importer AND metric = :metric AND date(timestamp) = :day",
            [{"importer": importer, "metric": metric, "day": day} for metric, day in touched],
        )
    conn.commit()
    return db_invariant.bulk_insert_measurements(conn, tagged)


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

    Always inserts a new row rather than upserting by date — a day can
    have many measurements of the same metric (multiple workouts,
    repeated heart-rate readings, etc.). Contrast
    upsert_daily_metric_measurements, which *does* upsert (by design —
    it represents a single declared daily total, not an accumulating
    series of readings). importer/imported_at record provenance for rows written by an
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


def insert_workout_session(
    conn: sqlite3.Connection,
    date: str,
    activity_type: str,
    duration_minutes: int,
    start_time: str | None = None,
    intensity: str | None = None,
    avg_heart_rate: int | None = None,
    max_heart_rate: int | None = None,
    source: str | None = None,
    notes: str | None = None,
) -> int:
    """Insert one workout_sessions row and return its id.

    Always inserts rather than upserting — a day can have more than one
    workout. `date` and `duration_minutes` are the only fields daily
    totals need; everything else is optional context that either isn't
    always known or isn't always tracked by every source. `intensity`,
    if given, must be one of WORKOUT_INTENSITIES (checked by callers
    such as server.py's log_workout_session, not here, so this stays
    usable from import_adapters.py for sources with their own scale).
    Requires a writable connection; commits before returning.
    """
    cursor = conn.execute(
        "INSERT INTO workout_sessions "
        "(date, activity_type, start_time, duration_minutes, intensity, "
        "avg_heart_rate, max_heart_rate, source, notes) "
        "VALUES (:date, :activity_type, :start_time, :duration_minutes, :intensity, "
        ":avg_heart_rate, :max_heart_rate, :source, :notes)",
        {
            "date": date,
            "activity_type": activity_type,
            "start_time": start_time,
            "duration_minutes": duration_minutes,
            "intensity": intensity,
            "avg_heart_rate": avg_heart_rate,
            "max_heart_rate": max_heart_rate,
            "source": source,
            "notes": notes,
        },
    )
    conn.commit()
    return cursor.lastrowid


def query_workout_sessions(
    conn: sqlite3.Connection,
    start: str | None = None,
    end: str | None = None,
    activity_type: str | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """Return workout_sessions rows matching the given filters, most
    recent day first (then most recently inserted within a day). start/end
    compare against `date` (YYYY-MM-DD) inclusively. All filters are
    optional; omitting them all returns the most recent `limit` sessions.
    """
    clauses, params = [], {}
    if start is not None:
        clauses.append("date >= :start")
        params["start"] = start
    if end is not None:
        clauses.append("date <= :end")
        params["end"] = end
    if activity_type is not None:
        clauses.append("activity_type = :activity_type")
        params["activity_type"] = activity_type
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params["limit"] = limit
    cursor = conn.cursor()
    cursor.row_factory = row_class()
    rows = cursor.execute(
        f"SELECT id, date, activity_type, start_time, duration_minutes, intensity, "
        f"avg_heart_rate, max_heart_rate, source, notes, created_at "
        f"FROM workout_sessions {where} ORDER BY date DESC, id DESC LIMIT :limit",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


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
    whenever both are present for that day. Without a priority list (or
    when none of its sources is present), falls back to the same order
    db/aggregation.py's projection uses: the source that observed the
    most distinct hours of the day, then the one with the latest
    observation, then source name (a missing source last). Hours are
    read from timestamp[11:13], which matches the projection for the
    naive timestamps the importers write. A single source (or no rows)
    is returned unchanged.
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

    def _stamps(source: str | None) -> list[str]:
        return [str(r.get("timestamp") or "") for r in rows if r.get("source") == source]

    # Three stable sorts, least significant first: name, then latest observation
    # (descending), then hours observed (descending).
    ordered = sorted(sources, key=lambda s: (s is None, s or ""))
    ordered.sort(key=lambda s: max(_stamps(s)), reverse=True)
    ordered.sort(key=lambda s: len({t[11:13] for t in _stamps(s) if t[11:13]}), reverse=True)
    return [r for r in rows if r.get("source") == ordered[0]], True


def aggregate_measurements_to_daily(
    conn: sqlite3.Connection, day: str, source_priority: list[str] | None = None
) -> dict[str, Any]:
    """Roll up one day's measurements into a daily_metrics-shaped preview
    dict (date + whichever metrics have measurements that day), reading
    each metric's aggregation method from the aggregation_rules table —
    the same canonical table db/aggregation.py's trigger-generated SQL
    reads, rather than a separately hand-copied Python dict, so there is
    exactly one place a metric's method is declared. Metrics with no rows
    for `day`, or no aggregation_rules entry, are omitted rather than
    written as null.

    Purely a read-only computation: it does NOT write daily_metrics
    itself (unlike before the measurements/daily_metrics invariant work —
    see db/schema.sql, db/invariant.py). daily_metrics is now a
    trigger-maintained projection that keeps one source's observations per
    (date, metric), chosen from the stored source_priority table (see
    db/aggregation.py), computed the moment a row is inserted/updated/
    deleted. This is a preview of what a *different* source_priority would
    produce, for comparison against daily_metrics' stored value — see
    server.py's aggregate_measurements tool, which surfaces both side by
    side. Given no source_priority it reproduces the stored value.

    When a metric has measurements from more than one source that day,
    resolve_source_conflicts picks which to keep for this preview (using
    source_priority if given, else the stored one) rather than blending
    readings from different devices into one number. See get_metric_provenance to
    inspect a disagreement before deciding on a priority.
    """
    methods = dict(conn.execute("SELECT metric, method FROM aggregation_rules").fetchall())

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

    stored_priority = db_invariant.get_source_priority(conn)
    result: dict[str, Any] = {"date": day}
    for metric, metric_rows in by_metric.items():
        how = methods.get(metric)
        if how is None:
            continue
        priority = source_priority or stored_priority.get(metric) or stored_priority.get("*")
        kept_rows, _conflict = resolve_source_conflicts(metric_rows, priority)
        values = [(r["value"], r["timestamp"]) for r in kept_rows]
        if how == "sum":
            result[metric] = sum(v for v, _ts in values)
        elif how == "mean":
            result[metric] = round(sum(v for v, _ts in values) / len(values), 1)
        elif how == "last":
            result[metric] = max(values, key=lambda pair: pair[1])[0]
    return result


def count_source_conflicts(conn: sqlite3.Connection, metric: str, start: date, end: date) -> int:
    """Count how many distinct days in [start, end] had a genuine
    multi-source conflict for `metric` -- i.e. days where
    resolve_source_conflicts had to pick a winner between 2+ distinct
    sources, not just multiple readings from the same source.

    Used by server.py's explain_metric_change to flag that a trend or
    baseline computed over this window is partly built on days where
    sources disagreed, without re-deriving that signal from raw
    measurements itself. Does not decide which reading is "right" -- this
    is purely a count of disagreement, for the caller to decide how to
    caveat downstream analytics.
    """
    rows = query_measurements(
        conn, metric=metric, start=start.isoformat(), end=end.isoformat() + "T23:59:59", limit=MAX_ROWS_RETURNED
    )
    by_day: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_day.setdefault(row["timestamp"][:10], []).append(row)

    conflicting_days = 0
    for day_rows in by_day.values():
        _kept, conflict = resolve_source_conflicts(day_rows)
        if conflict:
            conflicting_days += 1
    return conflicting_days


# Sanity bounds for each metric: (min, max, human label used in error messages).
# Deliberately generous — meant to catch obvious mistakes (unit confusion,
# a slipped decimal point, a fat-fingered extra digit) rather than to police
# what's "normal". mood is fixed at 1-10 so the scale is consistent across
# every log_daily_metric call, rather than left to whatever scale a given
# session happens to use.
# Derived from metric_registry.METRICS, where each metric's range and
# label are now declared; still exported under this name because
# server.py, sample_data and the tests import it from here.
METRIC_BOUNDS = metric_bounds()


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
