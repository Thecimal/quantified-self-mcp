"""
Quantified Self MCP Server
===========================

A local Model Context Protocol (MCP) server that lets an LLM query and log
your own health and finance data — without any of it leaving your machine.

Tools exposed:
- read_health_data, read_finance_data — read-only, unchanged.
- log_daily_metric / update_daily_metric / clear_daily_metric /
  clear_daily_metrics_range — full CRUD on daily_metrics.
- log_measurement / update_measurement / delete_measurement — full CRUD on
  arbitrary body measurements (weight, body_fat_pct, etc.).
- log_workout / update_workout / delete_workout — full CRUD on workouts.

Read tools open SQLite in read-only mode. Write tools open a separate
read-write connection, used only for the explicit mutation each tool
performs — there is no general-purpose SQL execution exposed to the model.

Test it on its own with the MCP Inspector:
    fastmcp dev inspector server.py

In normal use, this file is launched as a subprocess by an MCP client such
as Claude Desktop, which talks to it over stdio — see README.md.
"""

import json
import logging
import os
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
HEALTH_DB_PATH = Path(os.environ.get("HEALTH_DB_PATH", BASE_DIR / "data" / "health.db")).expanduser()
FINANCE_DB_PATH = Path(os.environ.get("FINANCE_DB_PATH", BASE_DIR / "data" / "finance.db")).expanduser()

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("quantified-self-mcp")

mcp = FastMCP("Quantified Self")

# ---------------------------------------------------------------------------
# Database schemas
# ---------------------------------------------------------------------------

HEALTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_metrics (
    date TEXT PRIMARY KEY,
    steps INTEGER,
    sleep_hours REAL,
    resting_heart_rate INTEGER
);

CREATE TABLE IF NOT EXISTS measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS workouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    type TEXT NOT NULL,
    duration_minutes REAL,
    calories REAL,
    notes TEXT
);
"""

FINANCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    category TEXT NOT NULL,
    amount REAL NOT NULL,
    description TEXT
);
"""

_SCHEMA_MAP: dict[Path, str] = {
    HEALTH_DB_PATH: HEALTH_SCHEMA,
    FINANCE_DB_PATH: FINANCE_SCHEMA,
}


def _ensure_db(db_path: Path) -> None:
    if db_path.exists():
        return
    db_path.parent.mkdir(parents=True, exist_ok=True)
    schema = _SCHEMA_MAP.get(db_path, "")
    conn = sqlite3.connect(str(db_path))
    if schema:
        conn.executescript(schema)
        conn.commit()
    conn.close()
    logger.info("Created empty database at %s — run init_db.py to populate it.", db_path)


def _ensure_tables(db_path: Path) -> None:
    """Run CREATE TABLE IF NOT EXISTS against an existing DB so new tables
    (measurements, workouts) get added to older health.db files."""
    schema = _SCHEMA_MAP.get(db_path, "")
    if not schema:
        return
    conn = sqlite3.connect(str(db_path))
    conn.executescript(schema)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Small helpers shared by tools
# ---------------------------------------------------------------------------

@contextmanager
def _readonly_connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open db_path read-only, so this connection can never write."""
    _ensure_db(db_path)
    _ensure_tables(db_path)
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _readwrite_connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open db_path read-write. Used only by the explicit write tools below —
    never exposed as general-purpose SQL execution to the model."""
    _ensure_db(db_path)
    _ensure_tables(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _parse_date(value: str, field_name: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{field_name} must be formatted YYYY-MM-DD, got {value!r}") from exc


def _resolve_range(
    start_date: str | None, end_date: str | None, default_days: int
) -> tuple[date, date]:
    end = _parse_date(end_date, "end_date") if end_date else date.today()
    start = _parse_date(start_date, "start_date") if start_date else end - timedelta(days=default_days)
    if start > end:
        raise ValueError(f"start_date ({start}) is after end_date ({end})")
    return start, end


def _numeric_stats(rows: list[dict[str, Any]], key: str) -> dict[str, float | None]:
    values = [r[key] for r in rows if r.get(key) is not None]
    if not values:
        return {"avg": None, "min": None, "max": None}
    return {"avg": round(sum(values) / len(values), 1), "min": min(values), "max": max(values)}


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------

@mcp.tool
def read_health_data(start_date: str | None = None, end_date: str | None = None) -> str:
    """Read daily steps, sleep hours, and resting heart rate."""
    start, end = _resolve_range(start_date, end_date, default_days=30)
    with _readonly_connection(HEALTH_DB_PATH) as conn:
        cursor = conn.execute(
            "SELECT date, steps, sleep_hours, resting_heart_rate "
            "FROM daily_metrics WHERE date BETWEEN ? AND ? ORDER BY date",
            (start.isoformat(), end.isoformat()),
        )
        rows = [dict(row) for row in cursor.fetchall()]
    result = {
        "range": {"start_date": start.isoformat(), "end_date": end.isoformat()},
        "rows": rows,
        "summary": {
            "days_with_data": len(rows),
            "steps": _numeric_stats(rows, "steps"),
            "sleep_hours": _numeric_stats(rows, "sleep_hours"),
            "resting_heart_rate": _numeric_stats(rows, "resting_heart_rate"),
        },
    }
    return json.dumps(result, indent=2)


@mcp.tool
def read_finance_data(
    start_date: str | None = None,
    end_date: str | None = None,
    category: str | None = None,
) -> str:
    """Read categorized expenses from the local finance ledger database."""
    start, end = _resolve_range(start_date, end_date, default_days=90)
    query = "SELECT date, category, amount, description FROM expenses WHERE date BETWEEN ? AND ?"
    params: list[Any] = [start.isoformat(), end.isoformat()]
    if category:
        query += " AND category = ? COLLATE NOCASE"
        params.append(category)
    query += " ORDER BY date"
    with _readonly_connection(FINANCE_DB_PATH) as conn:
        cursor = conn.execute(query, params)
        transactions = [dict(row) for row in cursor.fetchall()]
    total = 0.0
    by_category_raw: dict[str, float] = {}
    by_month_raw: dict[str, float] = {}
    for tx in transactions:
        total += tx["amount"]
        by_category_raw[tx["category"]] = by_category_raw.get(tx["category"], 0.0) + tx["amount"]
        month_key = tx["date"][:7]
        by_month_raw[month_key] = by_month_raw.get(month_key, 0.0) + tx["amount"]
    result = {
        "range": {"start_date": start.isoformat(), "end_date": end.isoformat()},
        "transactions": transactions,
        "summary": {
            "total_spent": round(total, 2),
            "by_category": {k: round(v, 2) for k, v in sorted(by_category_raw.items())},
            "by_month": {k: round(v, 2) for k, v in sorted(by_month_raw.items())},
        },
    }
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Write tools — daily_metrics
# ---------------------------------------------------------------------------

@mcp.tool
def log_daily_metric(
    date: str,
    steps: int | None = None,
    sleep_hours: float | None = None,
    resting_heart_rate: int | None = None,
) -> str:
    """
    Create or upsert a day's steps/sleep/resting heart rate. Omitted fields
    keep their existing stored value (or NULL if the row is new).
    """
    d = _parse_date(date, "date")
    with _readwrite_connection(HEALTH_DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO daily_metrics (date, steps, sleep_hours, resting_heart_rate)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                steps = COALESCE(excluded.steps, daily_metrics.steps),
                sleep_hours = COALESCE(excluded.sleep_hours, daily_metrics.sleep_hours),
                resting_heart_rate = COALESCE(excluded.resting_heart_rate, daily_metrics.resting_heart_rate)
            """,
            (d.isoformat(), steps, sleep_hours, resting_heart_rate),
        )
        row = conn.execute(
            "SELECT date, steps, sleep_hours, resting_heart_rate FROM daily_metrics WHERE date = ?",
            (d.isoformat(),),
        ).fetchone()
    return json.dumps(dict(row), indent=2)


@mcp.tool
def update_daily_metric(
    date: str,
    steps: int | None = None,
    sleep_hours: float | None = None,
    resting_heart_rate: int | None = None,
    clear_steps: bool = False,
    clear_sleep_hours: bool = False,
    clear_resting_heart_rate: bool = False,
) -> str:
    """
    Update fields on an existing daily_metrics row. Fails if the day doesn't
    exist. Pass clear_<field>=True to explicitly NULL that field.
    """
    d = _parse_date(date, "date")
    with _readwrite_connection(HEALTH_DB_PATH) as conn:
        existing = conn.execute("SELECT date FROM daily_metrics WHERE date = ?", (d.isoformat(),)).fetchone()
        if existing is None:
            raise ValueError(f"No daily_metrics row for {d.isoformat()}; use log_daily_metric to create it.")

        sets, params = [], []
        for field, value, clear_flag in (
            ("steps", steps, clear_steps),
            ("sleep_hours", sleep_hours, clear_sleep_hours),
            ("resting_heart_rate", resting_heart_rate, clear_resting_heart_rate),
        ):
            if clear_flag:
                sets.append(f"{field} = NULL")
            elif value is not None:
                sets.append(f"{field} = ?")
                params.append(value)

        if sets:
            params.append(d.isoformat())
            conn.execute(f"UPDATE daily_metrics SET {', '.join(sets)} WHERE date = ?", params)

        row = conn.execute(
            "SELECT date, steps, sleep_hours, resting_heart_rate FROM daily_metrics WHERE date = ?",
            (d.isoformat(),),
        ).fetchone()
    return json.dumps(dict(row), indent=2)


@mcp.tool
def clear_daily_metric(date: str) -> str:
    """Delete a single day's daily_metrics row."""
    d = _parse_date(date, "date")
    with _readwrite_connection(HEALTH_DB_PATH) as conn:
        cursor = conn.execute("DELETE FROM daily_metrics WHERE date = ?", (d.isoformat(),))
        deleted = cursor.rowcount
    return json.dumps({"date": d.isoformat(), "deleted": deleted > 0}, indent=2)


@mcp.tool
def clear_daily_metrics_range(start_date: str, end_date: str) -> str:
    """Bulk-delete daily_metrics rows in an inclusive date range."""
    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date")
    if start > end:
        raise ValueError(f"start_date ({start}) is after end_date ({end})")
    with _readwrite_connection(HEALTH_DB_PATH) as conn:
        cursor = conn.execute(
            "DELETE FROM daily_metrics WHERE date BETWEEN ? AND ?",
            (start.isoformat(), end.isoformat()),
        )
        deleted = cursor.rowcount
    return json.dumps({"start_date": start.isoformat(), "end_date": end.isoformat(), "deleted": deleted}, indent=2)


# ---------------------------------------------------------------------------
# Write tools — measurements
# ---------------------------------------------------------------------------

@mcp.tool
def log_measurement(
    date: str,
    metric_name: str,
    value: float,
    unit: str | None = None,
    notes: str | None = None,
) -> str:
    """Record a body measurement (e.g. weight, body_fat_pct, waist_cm)."""
    d = _parse_date(date, "date")
    with _readwrite_connection(HEALTH_DB_PATH) as conn:
        cursor = conn.execute(
            "INSERT INTO measurements (date, metric_name, value, unit, notes) VALUES (?, ?, ?, ?, ?)",
            (d.isoformat(), metric_name, value, unit, notes),
        )
        row = conn.execute(
            "SELECT id, date, metric_name, value, unit, notes FROM measurements WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
    return json.dumps(dict(row), indent=2)


@mcp.tool
def update_measurement(
    measurement_id: int,
    date: str | None = None,
    metric_name: str | None = None,
    value: float | None = None,
    unit: str | None = None,
    notes: str | None = None,
) -> str:
    """Update fields on an existing measurement row by id. Fails if id doesn't exist."""
    with _readwrite_connection(HEALTH_DB_PATH) as conn:
        existing = conn.execute("SELECT id FROM measurements WHERE id = ?", (measurement_id,)).fetchone()
        if existing is None:
            raise ValueError(f"No measurement with id {measurement_id}.")

        sets, params = [], []
        if date is not None:
            sets.append("date = ?")
            params.append(_parse_date(date, "date").isoformat())
        if metric_name is not None:
            sets.append("metric_name = ?")
            params.append(metric_name)
        if value is not None:
            sets.append("value = ?")
            params.append(value)
        if unit is not None:
            sets.append("unit = ?")
            params.append(unit)
        if notes is not None:
            sets.append("notes = ?")
            params.append(notes)

        if sets:
            params.append(measurement_id)
            conn.execute(f"UPDATE measurements SET {', '.join(sets)} WHERE id = ?", params)

        row = conn.execute(
            "SELECT id, date, metric_name, value, unit, notes FROM measurements WHERE id = ?",
            (measurement_id,),
        ).fetchone()
    return json.dumps(dict(row), indent=2)


@mcp.tool
def delete_measurement(measurement_id: int) -> str:
    """Delete a measurement row by id."""
    with _readwrite_connection(HEALTH_DB_PATH) as conn:
        cursor = conn.execute("DELETE FROM measurements WHERE id = ?", (measurement_id,))
        deleted = cursor.rowcount
    return json.dumps({"id": measurement_id, "deleted": deleted > 0}, indent=2)


# ---------------------------------------------------------------------------
# Write tools — workouts
# ---------------------------------------------------------------------------

@mcp.tool
def log_workout(
    date: str,
    type: str,
    duration_minutes: float | None = None,
    calories: float | None = None,
    notes: str | None = None,
) -> str:
    """Record a workout session."""
    d = _parse_date(date, "date")
    with _readwrite_connection(HEALTH_DB_PATH) as conn:
        cursor = conn.execute(
            "INSERT INTO workouts (date, type, duration_minutes, calories, notes) VALUES (?, ?, ?, ?, ?)",
            (d.isoformat(), type, duration_minutes, calories, notes),
        )
        row = conn.execute(
            "SELECT id, date, type, duration_minutes, calories, notes FROM workouts WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
    return json.dumps(dict(row), indent=2)


@mcp.tool
def update_workout(
    workout_id: int,
    date: str | None = None,
    type: str | None = None,
    duration_minutes: float | None = None,
    calories: float | None = None,
    notes: str | None = None,
) -> str:
    """Update fields on an existing workout row by id. Fails if id doesn't exist."""
    with _readwrite_connection(HEALTH_DB_PATH) as conn:
        existing = conn.execute("SELECT id FROM workouts WHERE id = ?", (workout_id,)).fetchone()
        if existing is None:
            raise ValueError(f"No workout with id {workout_id}.")

        sets, params = [], []
        if date is not None:
            sets.append("date = ?")
            params.append(_parse_date(date, "date").isoformat())
        if type is not None:
            sets.append("type = ?")
            params.append(type)
        if duration_minutes is not None:
            sets.append("duration_minutes = ?")
            params.append(duration_minutes)
        if calories is not None:
            sets.append("calories = ?")
            params.append(calories)
        if notes is not None:
            sets.append("notes = ?")
            params.append(notes)

        if sets:
            params.append(workout_id)
            conn.execute(f"UPDATE workouts SET {', '.join(sets)} WHERE id = ?", params)

        row = conn.execute(
            "SELECT id, date, type, duration_minutes, calories, notes FROM workouts WHERE id = ?",
            (workout_id,),
        ).fetchone()
    return json.dumps(dict(row), indent=2)


@mcp.tool
def delete_workout(workout_id: int) -> str:
    """Delete a workout row by id."""
    with _readwrite_connection(HEALTH_DB_PATH) as conn:
        cursor = conn.execute("DELETE FROM workouts WHERE id = ?", (workout_id,))
        deleted = cursor.rowcount
    return json.dumps({"id": workout_id, "deleted": deleted > 0}, indent=2)


if __name__ == "__main__":
    mcp.run()
