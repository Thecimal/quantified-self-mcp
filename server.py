"""
Quantified Self MCP Server
===========================

A local Model Context Protocol (MCP) server that lets an LLM query your own
health data — without any of it leaving your machine.

Tool exposed:
- read_health_data: daily steps, sleep hours, resting heart rate

Reads from a local SQLite file under ./data/ (created by init_db.py — see
README.md). This file makes no network calls, and the database connection
is opened in SQLite's read-only mode, so this process is physically
incapable of modifying your data or sending it anywhere.

Test it on its own with the MCP Inspector:
    fastmcp dev inspector server.py
(or `npx @modelcontextprotocol/inspector python server.py`, which works
regardless of which MCP framework a server is built with.)

In normal use, this file is launched as a subprocess by an MCP client
such as Claude Desktop, which talks to it over stdio — see README.md.

Note: finance support (read_finance_data) has been removed for now to keep
this server focused on health data. It's still in git history if you want
to bring it back later — see the commit that introduced this note.
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

# Can be overridden with an environment variable — handy if you'd rather
# point this at data living somewhere else on disk. Set this in the "env"
# block of your Claude Desktop config if you need to (see README.md).
BASE_DIR = Path(__file__).resolve().parent
HEALTH_DB_PATH = Path(os.environ.get("HEALTH_DB_PATH", BASE_DIR / "data" / "health.db")).expanduser()

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
# Database schema (mirrored from init_db.py so the server can self-bootstrap
# an empty DB in containerised / first-run environments)
# ---------------------------------------------------------------------------

HEALTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_metrics (
    date TEXT PRIMARY KEY,
    steps INTEGER,
    sleep_hours REAL,
    resting_heart_rate INTEGER
);
"""


def _ensure_db(db_path: Path) -> None:
    """Create the database with an empty schema if it doesn't exist yet.

    This allows the server to start cleanly in containerised or first-run
    environments (e.g. Glama) where init_db.py has not been run. Tools will
    return zero rows with a helpful note rather than crashing.
    """
    if db_path.exists():
        return
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(HEALTH_SCHEMA)
    conn.commit()
    conn.close()
    logger.info("Created empty database at %s — run init_db.py to populate it.", db_path)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

@contextmanager
def _readonly_connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open db_path in SQLite's read-only mode, so this process can never write to it."""
    _ensure_db(db_path)
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


if __name__ == "__main__":
    mcp.run()
