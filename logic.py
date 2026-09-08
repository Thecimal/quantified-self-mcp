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
MIGRATIONS: list[tuple[int, str, Callable[[sqlite3.Connection], None]]] = [
    (1, "create daily_metrics table", _migrate_v1_create_table),
    (2, "add weight_kg, workout_minutes, mood, water_ml columns", _migrate_v2_add_weight_workout_mood_water),
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
