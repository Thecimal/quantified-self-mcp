"""
Quantified Self MCP Server
===========================

A local Model Context Protocol (MCP) server that lets an LLM query your own
health data — without any of it leaving your machine.

Tools exposed:
- read_health_data: daily steps, sleep hours, resting heart rate, weight,
  workout minutes, mood, and water intake
- log_daily_metric: record one or more of those metrics for a given day
- clear_metric: blank out a single metric for a given day, undoing a bad
  log_daily_metric call

Reads from a local SQLite file under ./data/ (created by init_db.py — see
README.md). This file makes no network calls, so nothing you log or read
ever leaves your machine. read_health_data's connection is opened
read-only whenever possible, so that tool specifically cannot modify your
data; log_daily_metric and clear_metric are the deliberate exceptions, and
both only ever touch the daily_metrics table via a plain per-date
upsert/update — there is no way for any of these tools to run arbitrary SQL.

Test it on its own with the MCP Inspector:
    fastmcp dev inspector server.py
(or `npx @modelcontextprotocol/inspector python server.py`, which works
regardless of which MCP framework a server is built with.)

In normal use, this file is launched as a subprocess by an MCP client
such as Claude Desktop, which talks to it over stdio — see README.md.
"""

import logging
import os
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from logic import (
    BUSY_TIMEOUT_MS,
    MAX_ROWS_RETURNED,
    connect_writable,
    default_data_dir,
    ensure_schema,
    numeric_stats,
    parse_date,
    resolve_range,
    upsert_metrics,
    validate_metrics,
)

# The SQLite file never leaves this machine, but the *rows read out of it*
# do: whatever text a tool returns becomes part of the conversation sent to
# whichever LLM the MCP client is configured with. If that's a cloud-hosted
# model (as opposed to one running locally), your health data leaves your
# machine at that point, same as pasting it into a chat. This is true of any
# MCP server, not something specific to a bug here — so each tool's own
# docstring below ends with this exact paragraph (verbatim, so it shows up
# in the tool description an LLM/agent actually sees, not just in this
# file's own docs). Kept here too as the single source of truth for that
# wording, and to let tests/test_server.py assert every tool still carries
# it word-for-word rather than the warning silently drifting or being
# dropped by a future edit.
CLOUD_MODEL_WARNING = (
    "    Privacy note: this server and its SQLite file are entirely local, but\n"
    "    the data returned by this tool becomes part of the conversation sent\n"
    "    to whatever model the calling client is configured with. If that\n"
    "    model runs in the cloud rather than on your machine, treat this the\n"
    "    same as pasting the data into a chat with that provider."
)

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
# Output schemas
# ---------------------------------------------------------------------------
#
# Each tool below returns one of these Pydantic models instead of a
# hand-built dict passed through json.dumps. FastMCP derives a JSON output
# schema from the return-type annotation and populates the response's
# structured_content field to match it, in addition to the usual text
# content (still a JSON string, for clients that only read that) — so a
# client can validate/consume the result as a real typed object instead of
# re-parsing free-form text. See tests/test_server.py for a check of the
# actual wire-level structured_content via an in-memory fastmcp Client.


class DailyMetricsRow(BaseModel):
    date: str
    steps: int | None = None
    sleep_hours: float | None = None
    resting_heart_rate: int | None = None
    weight_kg: float | None = None
    workout_minutes: int | None = None
    mood: int | None = None
    water_ml: int | None = None


class DateRange(BaseModel):
    start_date: str
    end_date: str


class MetricStats(BaseModel):
    avg: float | None = None
    min: float | None = None
    max: float | None = None


class HealthDataSummary(BaseModel):
    days_with_data: int
    steps: MetricStats
    sleep_hours: MetricStats
    resting_heart_rate: MetricStats
    weight_kg: MetricStats
    workout_minutes: MetricStats
    mood: MetricStats
    water_ml: MetricStats


class ReadHealthDataResult(BaseModel):
    range: DateRange
    rows: list[DailyMetricsRow]
    truncated: bool
    summary: HealthDataSummary


class LogDailyMetricResult(BaseModel):
    logged: dict[str, int | float]
    row: DailyMetricsRow


class ClearMetricResult(BaseModel):
    cleared: str
    row: DailyMetricsRow | None = None
    note: str | None = None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Can be overridden with an environment variable — handy if you'd rather
# point this at data living somewhere else on disk. Set this in the "env"
# block of your Claude Desktop config if you need to (see README.md).
#
# The default itself adapts to how this project was installed: a source
# checkout gets data/ next to this file (matching README.md's "Project
# structure" and what CI expects); a system-wide `pip install` — where
# this file lives inside site-packages, not writable by an ordinary user
# — falls back to a per-user data directory instead. See
# logic.default_data_dir for the exact rule.
BASE_DIR = Path(__file__).resolve().parent
HEALTH_DB_PATH = Path(os.environ.get("HEALTH_DB_PATH", default_data_dir(BASE_DIR) / "health.db")).expanduser()

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
    conn = connect_writable(db_path)
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
    than the OS-level open flag. Either way, busy_timeout is set so a read
    landing at the exact instant a write commits waits briefly rather than
    failing immediately (WAL mode, enabled in logic.ensure_schema, makes
    this rare in the first place — readers don't normally block on a
    writer at all).
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
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Error semantics
# ---------------------------------------------------------------------------

# Stable, machine-parseable codes prefixed onto every ToolError message below
# (as "[code] human message"), so a client or the calling LLM can branch on
# the failure kind — e.g. retry on "database_locked" but not on
# "invalid_date" — without parsing free-form English. The human message
# after the code is still the primary content and is unchanged from before;
# existing substring-matching tests (e.g. on "mood") keep working since the
# code is a prefix, not a replacement.
ERR_INVALID_DATE = "invalid_date"
ERR_INVALID_RANGE = "invalid_range"
ERR_MISSING_METRIC = "missing_metric"
ERR_INVALID_METRIC_VALUE = "invalid_metric_value"
ERR_INVALID_FIELD = "invalid_field"
ERR_DATABASE_LOCKED = "database_locked"
ERR_DATABASE_ERROR = "database_error"


def _tool_error(code: str, message: str) -> ToolError:
    return ToolError(f"[{code}] {message}")


def _is_locked_error(exc: sqlite3.Error) -> bool:
    """True if exc looks like a lock/busy contention error rather than a
    missing/corrupt database — used to pick database_locked vs
    database_error so the two failure modes (retry-worthy vs not) are
    distinguishable by code, not just by re-reading the message text.
    """
    return isinstance(exc, sqlite3.OperationalError) and "lock" in str(exc).lower()


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        title="Read health data",
        readOnlyHint=True,  # opened via _readonly_connection; cannot write
        idempotentHint=True,  # same args -> same result, no side effects
        openWorldHint=False,  # only ever touches the local SQLite file
    )
)
def read_health_data(start_date: str | None = None, end_date: str | None = None) -> ReadHealthDataResult:
    """
    Read daily health metrics from the local database: steps, sleep hours,
    resting heart rate, weight (kg), workout minutes, mood, and water
    intake (ml).

    Args:
        start_date: First day to include, formatted YYYY-MM-DD.
            Defaults to 30 days before end_date. Ranges over ~10 years are rejected.
        end_date: Last day to include, formatted YYYY-MM-DD. Defaults to today.

    Returns:
        A ReadHealthDataResult with:
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

    Privacy note: this server and its SQLite file are entirely local, but
    the data returned by this tool becomes part of the conversation sent
    to whatever model the calling client is configured with. If that
    model runs in the cloud rather than on your machine, treat this the
    same as pasting the data into a chat with that provider.
    """
    try:
        start, end = resolve_range(start_date, end_date, default_days=30)
    except ValueError as exc:
        raise _tool_error(ERR_INVALID_RANGE, str(exc)) from exc

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
        if _is_locked_error(exc):
            raise _tool_error(
                ERR_DATABASE_LOCKED,
                "Could not read the health database — it is locked by another process. Try again in a moment.",
            ) from exc
        raise _tool_error(
            ERR_DATABASE_ERROR,
            "Could not read the health database — it may be missing or corrupt. Try again, or re-run init_db.py.",
        ) from exc

    truncated = len(rows) > MAX_ROWS_RETURNED
    returned_rows = rows[-MAX_ROWS_RETURNED:] if truncated else rows

    return ReadHealthDataResult(
        range=DateRange(start_date=start.isoformat(), end_date=end.isoformat()),
        rows=[DailyMetricsRow(**row) for row in returned_rows],
        truncated=truncated,
        summary=HealthDataSummary(
            days_with_data=len(rows),
            **{metric: MetricStats(**numeric_stats(rows, metric)) for metric in METRIC_COLUMNS},
        ),
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Log a daily metric",
        readOnlyHint=False,
        destructiveHint=False,  # upserts/overwrites values, never drops a row or column
        idempotentHint=True,  # re-sending the same values leaves the row unchanged
        openWorldHint=False,
    )
)
def log_daily_metric(
    date: str,
    steps: int | None = None,
    sleep_hours: float | None = None,
    resting_heart_rate: int | None = None,
    weight_kg: float | None = None,
    workout_minutes: int | None = None,
    mood: int | None = None,
    water_ml: int | None = None,
) -> LogDailyMetricResult:
    """
    Record one or more health metrics for a single day, creating that
    day's row if it doesn't already have one.

    Only the metrics you pass are written — anything left as null is not
    touched, so logging just today's mood doesn't erase today's steps if
    they were set earlier. To undo a value logged by mistake, use
    clear_metric rather than trying to overwrite it with a placeholder.

    Args:
        date: The day to log, formatted YYYY-MM-DD.
        steps: Step count for the day. 0-200,000.
        sleep_hours: Hours of sleep. 0-24.
        resting_heart_rate: Resting heart rate in bpm. 20-250.
        weight_kg: Body weight in kilograms. 1-500.
        workout_minutes: Minutes of exercise. 0-1,440.
        mood: Mood rating on a 1-10 scale.
        water_ml: Water intake in millilitres. 0-10,000.

    Returns:
        A LogDailyMetricResult with "logged" (just the fields this call set)
        and "row" (the day's full current state across all metrics,
        including any set previously).

    Privacy note: this server and its SQLite file are entirely local, but
    the data returned by this tool becomes part of the conversation sent
    to whatever model the calling client is configured with. If that
    model runs in the cloud rather than on your machine, treat this the
    same as pasting the data into a chat with that provider.
    """
    try:
        day = parse_date(date, "date")
    except ValueError as exc:
        raise _tool_error(ERR_INVALID_DATE, str(exc)) from exc

    provided = {
        k: v
        for k, v in {
            "steps": steps,
            "sleep_hours": sleep_hours,
            "resting_heart_rate": resting_heart_rate,
            "weight_kg": weight_kg,
            "workout_minutes": workout_minutes,
            "mood": mood,
            "water_ml": water_ml,
        }.items()
        if v is not None
    }
    if not provided:
        raise _tool_error(ERR_MISSING_METRIC, "Provide at least one metric to log alongside the date.")

    try:
        validate_metrics(provided)
    except ValueError as exc:
        raise _tool_error(ERR_INVALID_METRIC_VALUE, str(exc)) from exc

    try:
        conn = connect_writable(HEALTH_DB_PATH)
        try:
            ensure_schema(conn)
            upsert_metrics(conn, [{"date": day.isoformat(), **provided}])
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT date, " + ", ".join(METRIC_COLUMNS) + " FROM daily_metrics WHERE date = ?",
                (day.isoformat(),),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.error("Database error writing to %s: %s", HEALTH_DB_PATH, exc)
        code = ERR_DATABASE_LOCKED if _is_locked_error(exc) else ERR_DATABASE_ERROR
        raise _tool_error(
            code,
            "Could not write to the health database — it may be locked by another process. Try again in a moment.",
        ) from exc

    return LogDailyMetricResult(logged=provided, row=DailyMetricsRow(**dict(row)))


@mcp.tool(
    annotations=ToolAnnotations(
        title="Clear a single metric",
        readOnlyHint=False,
        destructiveHint=True,  # blanks out a previously logged value
        idempotentHint=True,  # clearing an already-null field is a no-op
        openWorldHint=False,
    )
)
def clear_metric(date: str, field: str) -> ClearMetricResult:
    """
    Blank out (set to null) a single metric for a single day, without
    touching that day's other metrics. The counterpart to log_daily_metric
    for undoing a bad value — e.g. a mood logged for the wrong day, or a
    weight entered with the wrong units.

    Args:
        date: The day to clear a field for, formatted YYYY-MM-DD.
        field: Which metric to blank out. One of: steps, sleep_hours,
            resting_heart_rate, weight_kg, workout_minutes, mood, water_ml.

    Returns:
        A ClearMetricResult with "cleared" (the field name) and "row" (the
        day's full current state after clearing). If no row exists yet
        for that date, "row" is null and "note" explains there was
        nothing to clear.

    Privacy note: this server and its SQLite file are entirely local, but
    the data returned by this tool becomes part of the conversation sent
    to whatever model the calling client is configured with. If that
    model runs in the cloud rather than on your machine, treat this the
    same as pasting the data into a chat with that provider.
    """
    try:
        day = parse_date(date, "date")
    except ValueError as exc:
        raise _tool_error(ERR_INVALID_DATE, str(exc)) from exc

    if field not in METRIC_COLUMNS:
        raise _tool_error(ERR_INVALID_FIELD, f"field must be one of: {', '.join(METRIC_COLUMNS)} — got {field!r}")

    try:
        conn = connect_writable(HEALTH_DB_PATH)
        try:
            ensure_schema(conn)
            conn.execute(f"UPDATE daily_metrics SET {field} = NULL WHERE date = ?", (day.isoformat(),))
            conn.commit()
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT date, " + ", ".join(METRIC_COLUMNS) + " FROM daily_metrics WHERE date = ?",
                (day.isoformat(),),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.error("Database error writing to %s: %s", HEALTH_DB_PATH, exc)
        code = ERR_DATABASE_LOCKED if _is_locked_error(exc) else ERR_DATABASE_ERROR
        raise _tool_error(
            code,
            "Could not write to the health database — it may be locked by another process. Try again in a moment.",
        ) from exc

    if row is None:
        return ClearMetricResult(cleared=field, note=f"No row exists for {day.isoformat()} — nothing to clear.")
    return ClearMetricResult(cleared=field, row=DailyMetricsRow(**dict(row)))


def main():
    mcp.run()


if __name__ == "__main__":
    main()
