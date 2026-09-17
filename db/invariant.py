"""Runtime entrypoints over the schema/trigger DDL in db/schema.sql and
db/aggregation.py. Keep this module thin: all aggregation semantics live in
aggregation.py so there is exactly one place to change them."""

import sqlite3
from pathlib import Path

from . import aggregation

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def bootstrap(conn: sqlite3.Connection) -> None:
    """Create tables (if needed) and (re)install the write-path triggers.
    Safe to call on every app startup — DROP TRIGGER IF EXISTS makes trigger
    installation idempotent."""
    conn.executescript(_SCHEMA_PATH.read_text())
    conn.executescript(aggregation.generate_trigger_sql())
    conn.commit()


def verify(conn: sqlite3.Connection) -> dict:
    """Read-only. Returns {"status": "ok"} or
    {"status": "mismatch", "issues": [...]}. Never mutates."""
    rows = conn.execute(aggregation.generate_verify_sql()).fetchall()
    issues = [{"date": r[0], "metric": r[1], "issue": r[2], "expected": r[3], "stored": r[4]} for r in rows]
    return {"status": "ok", "issues": []} if not issues else {"status": "mismatch", "issues": issues}


def repair(conn: sqlite3.Connection) -> dict:
    """Rebuild daily_metrics from raw measurements for every metric with a
    rule. Intended for bulk import / post-corruption recovery, not for
    normal writes (those are handled by triggers). Returns verify() after
    rebuilding so callers can confirm the invariant now holds."""
    conn.executescript(aggregation.generate_repair_sql())
    conn.commit()
    return verify(conn)
