"""
Quantified Self MCP Server
===========================
A local Model Context Protocol (MCP) server that lets an LLM query your own
health and finance data — without any of it leaving your machine.

Tools exposed:
    - read_health_data:  daily steps, sleep hours, resting heart rate
    - read_finance_data: a categorized ledger of expenses, with totals

Both tools read from local SQLite files under ./data/ (created by
init_db.py — see README.md). This file makes no network calls, and both
database connections are opened in SQLite's read-only mode, so this
process is physically incapable of modifying your data or sending it
anywhere.

Test it on its own with the MCP Inspector:
    fastmcp dev inspector server.py
(or `npx @modelcontextprotocol/inspector python server.py`, which works
regardless of which MCP framework a server is built with.)

In normal use, this file is launched as a subprocess by an MCP client
such as Claude Desktop, which talks to it over stdio — see README.md.
"""

import json
import logging
import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Optional

from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Both paths can be overridden with environment variables — handy if you'd
# rather point this at data living somewhere else on disk. Set these in the
# "env" block of your Claude Desktop config if you need to (see README.md).
BASE_DIR = Path(__file__).resolve().parent
HEALTH_DB_PATH = Path(os.environ.get("HEALTH_DB_PATH", BASE_DIR / "data" / "health.db")).expanduser()
FINANCE_DB_PATH = Path(os.environ.get("FINANCE_DB_PATH", BASE_DIR / "data" / "finance.db")).expanduser()

# This server talks to its client over stdio. Anything written to stdout
# (e.g. a stray print()) would corrupt that channel and break the
# connection, so all logging is routed to stderr instead, which is safe.
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("quantified-self-mcp")

mcp = FastMCP("Quantified Self")


# ---------------------------------------------------------------------------
# Small helpers shared by both tools
# ---------------------------------------------------------------------------
@contextmanager
def _readonly_connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open db_path in SQLite's read-only mode, so this process can never write to it."""
    if not db_path.exists():
        raise FileNotFoundError(
            f"No database found at {db_path}. Run init_db.py first to create it "
            f"from a CSV export (see README.md)."
        )
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _parse_date(value: str, field_name: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{field_name} must be formatted YYYY-MM-DD, got {value!r}") from exc


def _resolve_range(
    start_date: Optional[str], end_date: Optional[str], default_days: int
) -> tuple[date, date]:
    """Fill in sensible defaults for an open-ended date range and validate it."""
    end = _parse_date(end_date, "end_date") if end_date else date.today()
    start = _parse_date(start_date, "start_date") if start_date else end - timedelta(days=default_days)
    if start > end:
        raise ValueError(f"start_date ({start}) is after end_date ({end})")
    return start, end


def _numeric_stats(rows: list[dict[str, Any]], key: str) -> dict[str, Optional[float]]:
    values = [r[key] for r in rows if r.get(key) is not None]
    if not values:
        return {"avg": None, "min": None, "max": None}
    return {"avg": round(sum(values) / len(values), 1), "min": min(values), "max": max(values)}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@mcp.tool
def read_health_data(start_date: Optional[str] = None, end_date: Optional[str] = None) -> str:
    """
    Read daily steps, sleep hours, and resting heart rate from the local health database.

    Args:
        start_date: First day to include, formatted YYYY-MM-DD.
                    Defaults to 30 days before end_date.
        end_date: Last day to include, formatted YYYY-MM-DD. Defaults to today.

    Returns:
        A JSON string with:
          - "range": the start/end dates actually used
          - "rows": one entry per day that has data (date, steps, sleep_hours,
            resting_heart_rate) — days with no recorded data are simply absent
          - "summary": days_with_data plus avg/min/max for each metric over the range
    """
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
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    category: Optional[str] = None,
) -> str:
    """
    Read categorized expenses from the local finance ledger database.

    Args:
        start_date: First day to include, formatted YYYY-MM-DD.
                    Defaults to 90 days before end_date.
        end_date: Last day to include, formatted YYYY-MM-DD. Defaults to today.
        category: Optional category name to filter to (case-insensitive,
                  exact match — e.g. "Groceries"). Omit to include all categories.

    Returns:
        A JSON string with:
          - "range": the start/end dates actually used
          - "transactions": matching ledger rows (date, category, amount, description)
          - "summary": total_spent, plus totals broken down by category and by month
    """
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
        month_key = tx["date"][:7]  # "YYYY-MM"
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


if __name__ == "__main__":
    mcp.run()
