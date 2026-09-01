"""
logic.py
========
Framework-free helpers shared by server.py and init_db.py: date parsing,
date-range resolution, numeric aggregation, and the health database schema.

Deliberately dependency-free (standard library only) so it can be imported
and unit-tested without installing fastmcp — see tests/test_logic.py.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Optional

HEALTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_metrics (
    date TEXT PRIMARY KEY,
    steps INTEGER,
    sleep_hours REAL,
    resting_heart_rate INTEGER,
    weight_kg REAL,
    workout_minutes INTEGER,
    mood INTEGER,
    water_ml INTEGER
);
"""

# Columns added after the original release. Kept separate from HEALTH_SCHEMA
# (rather than just relying on CREATE TABLE) because CREATE TABLE IF NOT
# EXISTS does nothing for a daily_metrics table that already exists from an
# older version of this project — ensure_schema() below adds these to any
# such table so upgrading never requires deleting your database.
ADDED_COLUMNS = {
    "weight_kg": "REAL",
    "workout_minutes": "INTEGER",
    "mood": "INTEGER",
    "water_ml": "INTEGER",
}

# Guardrails for read_health_data (server.py).
MAX_RANGE_DAYS = 3660  # ~10 years — a wider request is almost certainly a mistake
MAX_ROWS_RETURNED = 400  # ~13 months of daily rows; "summary" still covers the full range


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create daily_metrics if it doesn't exist, and add any columns that
    were introduced after the table may have first been created.

    Safe and cheap to call on every startup/import — CREATE TABLE IF NOT
    EXISTS and the ALTER TABLE calls are both no-ops once already applied.
    Requires a writable connection; commits before returning.
    """
    conn.executescript(HEALTH_SCHEMA)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(daily_metrics)")}
    for name, sqltype in ADDED_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE daily_metrics ADD COLUMN {name} {sqltype}")
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


def parse_date(value: str, field_name: str) -> date:
    """Parse a YYYY-MM-DD string, raising a clear, client-facing ValueError otherwise."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{field_name} must be formatted YYYY-MM-DD, got {value!r}") from exc


def resolve_range(
    start_date: Optional[str], end_date: Optional[str], default_days: int
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


def numeric_stats(rows: list[dict[str, Any]], key: str) -> dict[str, Optional[float]]:
    values = [r[key] for r in rows if r.get(key) is not None]
    if not values:
        return {"avg": None, "min": None, "max": None}
    return {"avg": round(sum(values) / len(values), 1), "min": min(values), "max": max(values)}
