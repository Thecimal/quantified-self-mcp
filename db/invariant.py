"""Runtime entrypoints over the schema/trigger DDL in db/schema.sql and
db/aggregation.py. Keep this module thin: all aggregation semantics live in
aggregation.py so there is exactly one place to change them."""

import sqlite3
from pathlib import Path
from typing import Any

from . import aggregation

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def bootstrap(conn: sqlite3.Connection) -> None:
    """Create tables (if needed) and (re)install the write-path triggers.
    Safe to call on every app startup — DROP TRIGGER IF EXISTS makes trigger
    installation idempotent. This is the *only* place table/trigger DDL for
    measurements/daily_metrics/aggregation_rules should be issued from, so
    logic.py's ensure_schema calls into this rather than duplicating it."""
    conn.executescript(_SCHEMA_PATH.read_text())
    conn.executescript(aggregation.generate_trigger_sql())
    conn.commit()


def install_triggers(conn: sqlite3.Connection) -> None:
    """(Re)install just the write-path triggers, without touching table DDL.
    Used after a bulk import temporarily drops them (see bulk_insert_measurements)."""
    conn.executescript(aggregation.generate_trigger_sql())
    conn.commit()


def drop_triggers(conn: sqlite3.Connection) -> None:
    """Drop the three measurements triggers. SQLite has no DISABLE TRIGGER,
    so a large bulk load drops them outright, loads data with plain INSERTs
    (validated against aggregation_rules up front, in Python, since RAISE(ABORT)
    isn't there to catch an unsupported metric while they're down), and calls
    install_triggers() again afterward — see bulk_insert_measurements."""
    conn.executescript(
        """
        DROP TRIGGER IF EXISTS trg_measurements_ai;
        DROP TRIGGER IF EXISTS trg_measurements_au;
        DROP TRIGGER IF EXISTS trg_measurements_ad;
        """
    )
    conn.commit()


def verify(conn: sqlite3.Connection) -> dict:
    """Read-only. Returns one of:
      {"status": "ok", "issues": []}
      {"status": "unsupported_metric", "issues": [...]}
      {"status": "mismatch", "issues": [...]}
    Never mutates.

    "unsupported_metric" takes priority over "mismatch": if raw measurements
    exist for a metric with no aggregation_rules entry, there is no canonical
    value to diff against, so that's reported distinctly rather than folded
    into a generic mismatch (task P0 #13). Normal writes can never actually
    produce this case (the INSERT/UPDATE triggers RAISE(ABORT, ...) first),
    but verify() also has to catch a database that reached this state some
    other way (e.g. a restored backup, or a rule deleted after the fact —
    see #8), since it is the audit of last resort.
    """
    rows = conn.execute(aggregation.generate_verify_sql()).fetchall()
    issues = [{"date": r[0], "metric": r[1], "issue": r[2], "expected": r[3], "stored": r[4]} for r in rows]
    if not issues:
        return {"status": "ok", "issues": []}
    if any(i["issue"] == "unsupported_metric" for i in issues):
        return {"status": "unsupported_metric", "issues": issues}
    return {"status": "mismatch", "issues": issues}


def repair(conn: sqlite3.Connection) -> dict:
    """Rebuild daily_metrics from raw measurements for every metric with a
    rule. Intended for bulk import / post-corruption recovery, not for
    normal writes (those are handled by triggers). Returns verify() after
    rebuilding so callers can confirm the invariant now holds."""
    conn.executescript(aggregation.generate_repair_sql())
    conn.commit()
    return verify(conn)


def bulk_insert_measurements(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> dict:
    """Efficient path for large imports (task P0 #15): temporarily drop the
    write-path triggers, insert every row with one executemany, reinstall
    the triggers, then run a single repair() rather than recomputing the
    daily projection once per inserted row.

    Each row must have metric/value/timestamp; unit/source/source_type/
    importer/imported_at are optional (None if omitted). Every metric is
    checked against aggregation_rules *before* anything is inserted — with
    the triggers down there is no RAISE(ABORT) to catch this mid-batch, so
    the check happens up front in Python instead, and the whole batch is
    rejected together (matching #7: an unsupported metric must never create
    an orphaned raw measurement, bulk path included).

    Returns the verify() result after rebuilding, so a caller gets the same
    read-only confirmation repair() gives.
    """
    if not rows:
        return verify(conn)

    known = {r[0] for r in conn.execute("SELECT metric FROM aggregation_rules")}
    unsupported = sorted({row["metric"] for row in rows if row["metric"] not in known})
    if unsupported:
        raise ValueError(f"no aggregation_rules entry for metric(s): {', '.join(unsupported)}")

    drop_triggers(conn)
    try:
        conn.executemany(
            "INSERT INTO measurements (timestamp, metric, value, unit, source, source_type, importer, imported_at) "
            "VALUES (:timestamp, :metric, :value, :unit, :source, :source_type, :importer, :imported_at)",
            [
                {
                    "timestamp": row["timestamp"],
                    "metric": row["metric"],
                    "value": row["value"],
                    "unit": row.get("unit"),
                    "source": row.get("source"),
                    "source_type": row.get("source_type"),
                    "importer": row.get("importer"),
                    "imported_at": row.get("imported_at"),
                }
                for row in rows
            ],
        )
        conn.commit()
    finally:
        install_triggers(conn)
    return repair(conn)


def get_source_priority(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Every configured priority list, best source first: {"*": [...], metric: [...]}.
    A metric with its own list uses only that list; every other metric uses the
    "*" list. Empty when nothing is configured."""
    lists: dict[str, list[str]] = {}
    for metric, source in conn.execute("SELECT metric, source FROM source_priority ORDER BY metric, rank"):
        lists.setdefault(metric, []).append(source)
    return lists


def set_source_priority(conn: sqlite3.Connection, metric: str, sources: list[str]) -> dict:
    """Replace the priority list for `metric` ("*" = the default for every metric
    without its own list) with `sources`, best first; an empty list removes it.
    Re-projects the affected daily_metrics rows in the same transaction, so the
    projection never reflects a stale priority, and returns verify() afterwards.
    Raises ValueError, changing nothing, for an unknown metric or an empty or
    duplicated source name."""
    known = {r[0] for r in conn.execute("SELECT metric FROM aggregation_rules")}
    if metric != aggregation.GLOBAL_PRIORITY_SCOPE and metric not in known:
        raise ValueError(
            f"unknown metric {metric!r}: use '{aggregation.GLOBAL_PRIORITY_SCOPE}' or one of "
            f"{', '.join(sorted(known))}"
        )
    if any(not isinstance(s, str) or not s for s in sources):
        raise ValueError("source names must be non-empty strings")
    if len(set(sources)) != len(sources):
        raise ValueError("duplicate source name in priority list")

    targets = sorted(known) if metric == aggregation.GLOBAL_PRIORITY_SCOPE else [metric]
    delete_sql, insert_sql = aggregation.generate_scoped_repair_statements()
    try:
        conn.execute("DELETE FROM source_priority WHERE metric = ?", (metric,))
        conn.executemany(
            "INSERT INTO source_priority (metric, rank, source) VALUES (?, ?, ?)",
            [(metric, rank, source) for rank, source in enumerate(sources, start=1)],
        )
        for target in targets:
            conn.execute(delete_sql, {"metric": target})
            conn.execute(insert_sql, {"metric": target})
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return verify(conn)
