"""
Quantified Self MCP Server
===========================

A local Model Context Protocol (MCP) server that lets an LLM query your own
health data — without any of it leaving your machine.

Tool exposed:
- read_health_data: daily steps, sleep hours, resting heart rate, weight,
  workout minutes, mood, and water intake

Reads from a local SQLite file under ./data/ (created by init_db.py — see
README.md). This file makes no network calls, and the database connection
is opened read-only whenever possible, so this process cannot modify your
data or send it anywhere.

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
from pathlib import Path
from typing import Iterator, Optional

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from logic import MAX_ROWS_RETURNED, ensure_schema, numeric_stats, resolve_range

# All non-date columns in daily_metrics, in the order they're selected and
# reported — the single place to touch when another metric is added.
METRIC_COLUMNS = [
    "steps",
    "sleep_hours",
    "resting_heart_rate",
    "weight_kg",
    "workout_minutes",
    "mood",
    "water_ml",
]

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

# mask_error_details=True: an unexpected internal error (corrupt DB, disk
# issue, etc.) is reduced to a generic message instead of leaking a raw
# Python traceback — including local file paths — to whatever LLM is
# calling this tool. Errors the model can actually act on (bad date format,
# a too-wide range, a locked database) are raised as ToolError below, and
# ToolError messages are always delivered to the client in full regardless
# of this setting.
mcp = FastMCP("Quantified Self", mask_error_details=True)

# ---------------------------------------------------------------------------
# Database bootstrap
# ---------------------------------------------------------------------------


def _ensure_db(db_path: Path) -> None:
    """Create the database if it doesn't exist yet, and migrate it to the
    current schema either way (adds any columns introduced since the file
    was first created — see logic.ensure_schema).

    This allows the server to start cleanly in containerised or first-run
    environments (e.g. Glama) where init_db.py has not been run. The tool
    will return zero rows with a helpful note rather than crashing. It also
    means upgrading this project never requires deleting an existing
    database — old rows keep their values, new columns just read as null
    until you log data for them.
    """
    is_new = not db_path.exists()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        ensure_schema(conn)
    finally:
        conn.close()
    if is_new:
        logger.info("Created empty database at %s — run init_db.py to populate it.", db_path)


@contextmanager
def _readonly_connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open db_path read-only so this process cannot write to it.

    Prefers SQLite's URI mode=ro, which enforces this at the driver level.
    Falls back to a normal connection guarded by PRAGMA query_only if
    mode=ro fails to open the file — which happens if a previous write left
    a WAL/journal file pending recovery, something SQLite refuses to do
    while read-only. The fallback still blocks writes, just via SQL rather
    than the OS-level open flag.
    """
    _ensure_db(db_path)
    uri = db_path.resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.execute("SELECT 1")  # force the open now, not on the caller's first real query
    except sqlite3.OperationalError:
        logger.warning(
            "Could not open %s read-only (likely a pending WAL/journal); "
            "falling back to a query_only connection.",
            db_path,
        )
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA query_only = ON")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool
def read_health_data(start_date: Optional[str] = None, end_date: Optional[str] = None) -> str:
    """
    Read daily health metrics from the local database: steps, sleep hours,
    resting heart rate, weight (kg), workout minutes, mood, and water
    intake (ml).

    Args:
        start_date: First day to include, formatted YYYY-MM-DD.
            Defaults to 30 days before end_date. Ranges over ~10 years are rejected.
        end_date: Last day to include, formatted YYYY-MM-DD. Defaults to today.

    Returns:
        A JSON string with:
        - "range": the start/end dates actually used
        - "rows": one entry per day that has at least one recorded metric
          (date plus whichever of steps, sleep_hours, resting_heart_rate,
          weight_kg, workout_minutes, mood, water_ml were logged for that
          day — fields with no data are null, not absent). Days with no
          data at all are simply absent from "rows". Capped at the most
          recent 400 matching days; see "truncated".
        - "truncated": true if more matching days existed than were returned in "rows"
        - "summary": days_with_data plus avg/min/max for each metric, computed
          over *all* matching days even when "rows" is truncated
    """
    try:
        start, end = resolve_range(start_date, end_date, default_days=30)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc

    try:
        with _readonly_connection(HEALTH_DB_PATH) as conn:
            cursor = conn.execute(
                "SELECT date, " + ", ".join(METRIC_COLUMNS) + " "
                "FROM daily_metrics WHERE date BETWEEN ? AND ? ORDER BY date",
                (start.isoformat(), end.isoformat()),
            )
            rows = [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as exc:
        logger.error("Database error reading %s: %s", HEALTH_DB_PATH, exc)
        raise ToolError(
            "Could not read the health database — it may be locked by another "
            "process, or missing/corrupt. Try again, or re-run init_db.py."
        ) from exc

    truncated = len(rows) > MAX_ROWS_RETURNED
    returned_rows = rows[-MAX_ROWS_RETURNED:] if truncated else rows

    result = {
        "range": {"start_date": start.isoformat(), "end_date": end.isoformat()},
        "rows": returned_rows,
        "truncated": truncated,
        "summary": {
            "days_with_data": len(rows),
            **{metric: numeric_stats(rows, metric) for metric in METRIC_COLUMNS},
        },
    }
    return json.dumps(result, indent=2)


if __name__ == "__main__":
    mcp.run()
