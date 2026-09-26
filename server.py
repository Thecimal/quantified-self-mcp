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
- log_measurement: record a single raw, timestamped observation (with
  source/unit) instead of a whole day's summary
- read_measurements: read back raw measurement rows, filterable by
  metric/date range/source
- aggregate_measurements: preview what a source-priority resolution of a
  day's raw measurements would look like, alongside daily_metrics' actual
  (all-sources) current value for that day
- get_metric_provenance: break a metric's readings for a day down by
  source, to spot when two sources disagree

Resources exposed (read-only, addressed by URI rather than invoked):
- health://metrics/schema: valid range and privacy status for each metric
- health://day/{date}: one day's metrics, equivalent to read_health_data
  with start_date == end_date == date

Reads from a local SQLite file under ./data/ (created by init_db.py — see
README.md). This file makes no network calls, so nothing you log or read
ever leaves your machine. read_health_data's connection is opened
read-only whenever possible, so that tool specifically cannot modify your
data; log_daily_metric and clear_metric are the deliberate exceptions.
Neither writes daily_metrics directly — it's a database-maintained
projection over the measurements table (see db/schema.sql,
db/invariant.py), kept in sync by SQLite triggers the moment a
measurement is written; log_daily_metric/clear_metric/log_measurement
only ever insert/delete plain measurements rows — there is no way for
any of these tools to run arbitrary SQL.

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
from datetime import date as date_type
from datetime import timedelta
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.exceptions import ResourceError
from mcp.types import ToolAnnotations

from analytics import (
    Point,
    calculate_trend,
    compare_periods,
    detect_anomalies,
    find_correlations,
)
from analytics import baseline as compute_baseline
from errors import (
    ERR_DATABASE_ERROR,
    ERR_DATABASE_LOCKED,
    ERR_INVALID_ACTIVITY_TYPE,
    ERR_INVALID_DATE,
    ERR_INVALID_DURATION,
    ERR_INVALID_FIELD,
    ERR_INVALID_INTENSITY,
    ERR_INVALID_RANGE,
    _is_locked_error,
    _tool_error,
)
from evidence import build_evidence
from logic import (
    METRIC_BOUNDS,
    WORKOUT_INTENSITIES,
    connect_writable,
    count_source_conflicts,
    daily_metrics_wide,
    db_error_types,
    default_data_dir,
    ensure_schema,
    insert_workout_session,
    parse_date,
    query_workout_sessions,
    resolve_range,
    row_class,
)
from logic import (
    readonly_connection as _logic_readonly_connection,
)
from metric_registry import METRIC_KEYS
from privacy import (
    INT_METRIC_COLUMNS,  # noqa: F401 -- unused here, but tests/test_metric_registry.py reaches it via server.INT_METRIC_COLUMNS
    PRIVATE_FIELDS,
    _redact_private_fields,
)
from qs_evidence import (
    Assessment,
    assess_anomaly,
    assess_baseline,
    assess_correlation,
    assess_trend,
    assess_window_comparison,
    combine_decisions,
)
from schemas import (
    AnomalyPoint,
    BaselineStats,
    CalculateTrendResult,
    ChangeNote,
    ClaimDecisionOut,
    ClaimEvidence,
    ClaimEvidenceComparative,
    ClaimFields,  # noqa: F401 -- unused here, but tests/test_claim_contract.py reaches it via server.ClaimFields
    ComparePeriodsResult,
    CorrelationResult,
    DailyMetricsRow,
    DateRange,
    DetectAnomaliesResult,
    Evidence,
    ExplainMetricChangeResult,
    GetBaselineResult,
    GetMetricHistoryResult,
    GetRecentChangesResult,
    HealthDataSummary,  # noqa: F401 -- unused here, but tests/test_metric_registry.py reaches it via server.HealthDataSummary
    LogWorkoutSessionResult,
    MetricDefinition,
    MetricSeriesPoint,
    ReadWorkoutSessionsResult,
    TrendStats,
    WorkoutSessionRow,
)
from tools.health import register_health_tools
from tools.measurements import register_measurement_tools

# The SQLite file never leaves this machine, but the *rows read out of it*
# do: whatever text a tool returns becomes part of the conversation sent to
# whichever LLM the MCP client is configured with. If that's a cloud-hosted
# model (as opposed to one running locally), your health data leaves your
# machine at that point, same as pasting it into a chat. This is true of any
# MCP server, not something specific to a bug here — so each tool's own
# docstring below places this exact paragraph right after its opening
# summary, before "Args:" (verbatim, so it shows up in the tool description
# an LLM/agent actually sees — see tests/test_server.py's comment on why
# position matters here, not just wording). Kept here too as the single
# source of truth for that wording, and to let tests/test_server.py assert
# every registered tool's *protocol-level* description still carries it
# word-for-word rather than the warning silently drifting, moving to a
# position that gets dropped, or being removed by a future edit.
#
# Written without the docstrings' own 4-space indentation on continuation
# lines: FastMCP derives each tool's description via inspect.getdoc(), which
# dedents a docstring before parsing it, so that indentation never survives
# into what a client actually receives — matching against the undedented
# form here would silently never match.
CLOUD_MODEL_WARNING = (
    "Privacy note: this server and its SQLite file are entirely local, but\n"
    "the data returned by this tool becomes part of the conversation sent\n"
    "to whatever model the calling client is configured with. If that\n"
    "model runs in the cloud rather than on your machine, treat this the\n"
    "same as pasting the data into a chat with that provider."
)

# All non-date columns in daily_metrics, in the order they're selected and
# reported. Derived from metric_registry.METRICS; the DailyMetricsRow and
# HealthDataSummary models and log_daily_metric's parameters below still
# list each metric by hand (tests/test_metric_registry.py fails if the
# models drift from the registry).
METRIC_COLUMNS = list(METRIC_KEYS)

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
TOOL_ROUTING_INSTRUCTIONS = (
    "TOOL ROUTING:\n"
    "\n"
    "For recording data:\n"
    "- simple daily metric (one value/day, e.g. steps, weight, mood) -> log_daily_metric\n"
    "- individual timestamped observation (has its own time/source, or a day may have several) -> log_measurement\n"
    "- workout/exercise session -> log_workout_session\n"
    "\n"
    "For retrieving data:\n"
    "- broad/general health data across multiple metrics -> read_health_data\n"
    "- individual raw measurement rows -> read_measurements\n"
    "- workout sessions -> read_workout_sessions\n"
    "- history/trend of one specific metric -> get_metric_history\n"
    "\n"
    "For analysis (why/trend/change/comparison/correlation):\n"
    "- why did one specific metric look like that on a given day -> explain_metric_change\n"
    "- broad scan of what changed lately across all metrics -> get_recent_changes\n"
    "- where a value came from, or whether sources agree -> get_metric_provenance\n"
)

mcp = FastMCP("Quantified Self", instructions=TOOL_ROUTING_INSTRUCTIONS, mask_error_details=True)

# ASGI app for platforms that run this module directly under an ASGI server
# (e.g. `uvicorn server:app`), rather than invoking `python server.py`.
# Manufact's auto-detected Python build does exactly this, so this needs to
# exist at import time regardless of how main() below is invoked.
app = mcp.http_app()

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
    """Ensure db_path exists (creating an empty, migrated database if this
    is a first run — see _ensure_db), then open it read-only. The actual
    read-only-opening logic (including optional SQLCipher decryption) now
    lives in logic.readonly_connection, since none of it is MCP-specific;
    this wrapper just adds the create-on-first-run behavior server.py
    itself needs.
    """
    _ensure_db(db_path)
    with _logic_readonly_connection(db_path) as conn:
        yield conn


def _fetch_metric_series(metric: str, start: date_type, end: date_type) -> list[Point]:
    """Read a single metric's (date, value) series from daily_metrics,
    skipping days where it's null.

    Every Layer-2/Layer-3 tool below goes through this, so each gets the
    same two guardrails read_health_data already applies: an unrecognized
    metric name is rejected the same as an invalid_field error elsewhere in
    this file, and a metric listed in HEALTH_PRIVATE_FIELDS is refused
    outright rather than merely redacted — a baseline, trend, or anomaly
    flag computed from a private metric would leak its *shape* to the
    calling model even if the raw values were nulled out afterward, so
    "private" has to mean "never fed into analytics," not just "never
    printed raw."
    """
    if metric not in METRIC_COLUMNS:
        raise _tool_error(ERR_INVALID_FIELD, f"metric must be one of: {', '.join(METRIC_COLUMNS)} — got {metric!r}")
    if metric in PRIVATE_FIELDS:
        raise _tool_error(
            ERR_INVALID_FIELD,
            f"{metric!r} is configured as private (HEALTH_PRIVATE_FIELDS) and can't be analyzed.",
        )
    try:
        with _readonly_connection(HEALTH_DB_PATH) as conn:
            rows = daily_metrics_wide(conn, [metric], start.isoformat(), end.isoformat())
    except db_error_types() as exc:
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
    return [Point(parse_date(row["date"], "date"), float(row[metric])) for row in rows]


def _baseline_stats(series: list[Point]) -> BaselineStats:
    return BaselineStats(**compute_baseline(series))


def _trend_stats(series: list[Point]) -> TrendStats:
    return TrendStats(**calculate_trend(series))


def _claim_fields(assessment: Assessment, prefix: str = "") -> dict:
    """Result-model kwargs for one assessed claim (see qs_evidence.assess)."""
    return {
        f"{prefix}evidence_profile": assessment.profile,
        f"{prefix}claim_decision": ClaimDecisionOut(**assessment.decision.to_mcp()),
    }


def _claim_evidence(assessment: Assessment, evidence: Evidence) -> ClaimEvidence:
    """Canonical ClaimEvidence envelope for one assessed claim."""
    return ClaimEvidence(
        evidence=evidence,
        profile=assessment.profile,
        decision=ClaimDecisionOut(**assessment.decision.to_mcp()),
    )


def _migrated_claim_fields(assessment: Assessment, evidence: Evidence) -> dict:
    """Result-model kwargs for a tool that has adopted ClaimEvidence: the canonical `claim` envelope
    plus the deprecated flat fields, built as exact mirrors of `claim` (never independently derived)
    so the two representations cannot silently diverge during the deprecation window. Used by
    detect_metric_anomalies, calculate_metric_trend, and each ChangeNote built in get_recent_changes
    (get_baseline builds `claim` directly instead). `_claim_fields` is now unused dead code, kept only
    until ClaimFields itself is removed in the post-deprecation cleanup pass."""
    claim = _claim_evidence(assessment, evidence)
    return {
        "claim": claim,
        "evidence_profile": claim.profile,
        "claim_decision": claim.decision,
    }


def _claim_evidence_comparative(
    assessment: Assessment, evidence_a: Evidence, evidence_b: Evidence
) -> ClaimEvidenceComparative:
    """Canonical ClaimEvidenceComparative envelope for one assessed claim resting on two source coverages."""
    return ClaimEvidenceComparative(
        evidence_a=evidence_a,
        evidence_b=evidence_b,
        profile=assessment.profile,
        decision=ClaimDecisionOut(**assessment.decision.to_mcp()),
    )


def _migrated_claim_fields_comparative(assessment: Assessment, evidence_a: Evidence, evidence_b: Evidence) -> dict:
    """Result-model kwargs for a two-source tool that has adopted ClaimEvidenceComparative: the canonical
    `claim` envelope plus the deprecated flat fields, built as exact mirrors of `claim` (never independently
    derived) so the two representations cannot silently diverge during the deprecation window. Used by
    find_metric_correlation (ComparePeriodsResult builds `claim` via `_claim_evidence_comparative` directly,
    and derives its own differently-named deprecated evidence_a/evidence_b-equivalent fields instead)."""
    claim = _claim_evidence_comparative(assessment, evidence_a, evidence_b)
    return {
        "claim": claim,
        "evidence_a": claim.evidence_a,
        "evidence_b": claim.evidence_b,
        "evidence_profile": claim.profile,
        "claim_decision": claim.decision,
    }


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
#
# Annotation conventions (all four hints are set explicitly on every tool;
# tests/test_mcp_client_integration.py pins the exact values over the wire):
#   - readOnlyHint: True only if the tool never writes health data or files.
#   - destructiveHint: True if a call can overwrite or remove existing state
#     (a replaced value or file counts, not only a deleted row).
#   - idempotentHint: True if repeating the same call leaves the same state.
#   - openWorldHint: always False; everything is local SQLite + local disk.
# Read-only tools may still trigger _ensure_db / ensure_schema first (creating
# an empty database on first run, applying pending migrations, reinstalling
# triggers). That is idempotent schema housekeeping that never changes
# recorded health data, so it does not make a read tool non-read-only.


read_health_data, export_health_data_csv, log_daily_metric, clear_metric = register_health_tools(
    mcp,
    db_path=HEALTH_DB_PATH,
    logger=logger,
    readonly_connection=_readonly_connection,
    metric_columns=METRIC_COLUMNS,
)


# ---------------------------------------------------------------------------
# Raw measurements
# ---------------------------------------------------------------------------
#
# daily_metrics (above) is one row per day — good for "how many steps
# today" but not for "why did resting heart rate jump after that workout",
# which needs the individual observations: when each was taken, and where
# it came from. measurements is that finer-grained layer: every
# log_measurement call is its own row, never upserted over a previous one,
# so a day can hold several readings of the same metric. daily_metrics
# itself is a database-maintained projection over measurements (see
# db/schema.sql, db/invariant.py) — every log_measurement/log_daily_metric/
# import automatically keeps it in sync, using each metric's
# aggregation_rules method (sum/mean/last); nothing here needs to (or
# can) push a value into it by hand. aggregate_measurements previews what
# a source_priority resolution of a day's measurements would look like,
# without writing anything.


log_measurement, read_measurements, aggregate_measurements, get_metric_provenance = register_measurement_tools(
    mcp,
    db_path=HEALTH_DB_PATH,
    logger=logger,
    readonly_connection=_readonly_connection,
    metric_columns=METRIC_COLUMNS,
)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Log a workout session",
        readOnlyHint=False,
        destructiveHint=False,  # always inserts a new row, never overwrites one
        idempotentHint=False,  # calling it twice logs two sessions, not one
        openWorldHint=False,
    )
)
def log_workout_session(
    date: str,
    activity_type: str,
    duration_minutes: int,
    start_time: str | None = None,
    intensity: str | None = None,
    avg_heart_rate: int | None = None,
    max_heart_rate: int | None = None,
    source: str | None = None,
    notes: str | None = None,
) -> LogWorkoutSessionResult:
    """
    Record one workout as a structured event — activity, timing, intensity,
    and heart-rate response — rather than folding it into the day's
    workout_minutes total. Use this alongside (not instead of)
    log_daily_metric/log_measurement for workout_minutes: this is what lets
    explain_metric_change say *what* the workout was, not just how long it
    ran. A day can have more than one session; each call adds a new row.

    Use this tool when:
    - the user describes an actual workout/exercise session (e.g. "I went
      running for 40 minutes", "log today's strength workout").

    Do not use this tool when:
    - the user only wants to record the day's total exercise minutes as a
      single number, with no activity type/timing/intensity -> use
      `log_daily_metric` (workout_minutes) or `log_measurement` instead.

    Privacy note: this server and its SQLite file are entirely local, but
    the data returned by this tool becomes part of the conversation sent
    to whatever model the calling client is configured with. If that
    model runs in the cloud rather than on your machine, treat this the
    same as pasting the data into a chat with that provider.

    Args:
        date: The day the workout happened, YYYY-MM-DD.
        activity_type: What kind of workout, e.g. "running", "cycling",
            "strength". Free-form.
        duration_minutes: How long it lasted, in minutes.
        start_time: When it started, HH:MM (24-hour) or a full ISO
            timestamp. Optional.
        intensity: One of "low", "moderate", "high". Optional.
        avg_heart_rate: Average heart rate during the workout, bpm. Optional.
        max_heart_rate: Peak heart rate during the workout, bpm. Optional.
        source: Where this came from, e.g. "Apple Watch", "manual". Optional.
        notes: Free-text notes, e.g. route or how it felt. Optional.

    Returns:
        A LogWorkoutSessionResult with the stored row, including its new id.
    """
    try:
        parse_date(date, "date")
    except ValueError as exc:
        raise _tool_error(ERR_INVALID_DATE, str(exc)) from exc
    if not activity_type.strip():
        raise _tool_error(ERR_INVALID_ACTIVITY_TYPE, "activity_type must be a non-empty string.")
    if duration_minutes <= 0:
        raise _tool_error(ERR_INVALID_DURATION, "duration_minutes must be a positive integer.")
    if intensity is not None and intensity not in WORKOUT_INTENSITIES:
        raise _tool_error(
            ERR_INVALID_INTENSITY,
            f"intensity must be one of {sorted(WORKOUT_INTENSITIES)}, got {intensity!r}.",
        )

    try:
        conn = connect_writable(HEALTH_DB_PATH)
        try:
            ensure_schema(conn)
            new_id = insert_workout_session(
                conn,
                date=date,
                activity_type=activity_type,
                duration_minutes=duration_minutes,
                start_time=start_time,
                intensity=intensity,
                avg_heart_rate=avg_heart_rate,
                max_heart_rate=max_heart_rate,
                source=source,
                notes=notes,
            )
            conn.row_factory = row_class()
            row = conn.execute("SELECT * FROM workout_sessions WHERE id = ?", (new_id,)).fetchone()
        finally:
            conn.close()
    except db_error_types() as exc:
        logger.error("Database error writing to %s: %s", HEALTH_DB_PATH, exc)
        code = ERR_DATABASE_LOCKED if _is_locked_error(exc) else ERR_DATABASE_ERROR
        raise _tool_error(
            code,
            "Could not write to the health database — it may be locked by another process. Try again in a moment.",
        ) from exc

    return LogWorkoutSessionResult(session=WorkoutSessionRow(**dict(row)))


@mcp.tool(
    annotations=ToolAnnotations(
        title="Read workout sessions",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def read_workout_sessions(
    start_date: str | None = None,
    end_date: str | None = None,
    activity_type: str | None = None,
    limit: int = 200,
) -> ReadWorkoutSessionsResult:
    """
    Read individual workout sessions (not the daily_metrics
    workout_minutes total), most recent day first. Use this to see what
    each workout actually was — activity, timing, intensity, heart rate —
    rather than just a day's summed minutes.

    Use this tool when:
    - the user asks about workouts/exercise sessions specifically (e.g.
      "what workouts did I do this week?", "show my recent gym sessions").

    Do not use this tool when:
    - the user just wants the daily workout_minutes total, not individual
      sessions -> use `read_health_data` or `get_metric_history` instead.

    Privacy note: this server and its SQLite file are entirely local, but
    the data returned by this tool becomes part of the conversation sent
    to whatever model the calling client is configured with. If that
    model runs in the cloud rather than on your machine, treat this the
    same as pasting the data into a chat with that provider.

    Args:
        start_date: Only return sessions on/after this date (YYYY-MM-DD). Omit for no lower bound.
        end_date: Only return sessions on/before this date (YYYY-MM-DD). Omit for no upper bound.
        activity_type: Only return sessions of this activity type. Omit for all types.
        limit: Maximum rows to return (default 200).

    Returns:
        A ReadWorkoutSessionsResult with the matching rows and a count.
    """
    try:
        conn = connect_writable(HEALTH_DB_PATH)
        try:
            ensure_schema(conn)
            conn.row_factory = row_class()
            rows = query_workout_sessions(
                conn, start=start_date, end=end_date, activity_type=activity_type, limit=limit
            )
        finally:
            conn.close()
    except db_error_types() as exc:
        logger.error("Database error reading from %s: %s", HEALTH_DB_PATH, exc)
        raise _tool_error(ERR_DATABASE_ERROR, "Could not read the health database. Try again in a moment.") from exc

    return ReadWorkoutSessionsResult(sessions=[WorkoutSessionRow(**row) for row in rows], count=len(rows))


# ---------------------------------------------------------------------------
# Layer 2: Analytics
# ---------------------------------------------------------------------------
#
# Everything here is a thin MCP wrapper around a pure function in
# analytics.py: fetch one metric's series with _fetch_metric_series (which
# enforces HEALTH_PRIVATE_FIELDS), hand it to the analytics function, wrap
# the result in a Pydantic model. None of these tools call each other over
# MCP — they share plain Python functions instead, the same pattern
# log_daily_metric/clear_metric already use for logic.py.


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get metric history",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def get_metric_history(
    metric: str, start_date: str | None = None, end_date: str | None = None
) -> GetMetricHistoryResult:
    """
    Read one metric's day-by-day values, without the other six metrics
    read_health_data always includes. Use this when you only care about a
    single metric (e.g. before calling get_baseline or calculate_metric_trend
    yourself) and don't need the full multi-metric payload.

    Use this tool when:
    - the user wants one specific metric's history/trend over time (e.g.
      "show my weight over the last 30 days", "how has resting heart rate
      changed this month").

    Do not use this tool when:
    - the request is broad/general, across multiple metrics at once -> use
      `read_health_data` instead.

    Privacy note: this server and its SQLite file are entirely local, but
    the data returned by this tool becomes part of the conversation sent
    to whatever model the calling client is configured with. If that
    model runs in the cloud rather than on your machine, treat this the
    same as pasting the data into a chat with that provider.

    Args:
        metric: One of steps, sleep_hours, resting_heart_rate, weight_kg,
            workout_minutes, mood, water_ml, heart_rate, hrv_ms. Rejected if configured as
            private via HEALTH_PRIVATE_FIELDS.
        start_date: First day to include, formatted YYYY-MM-DD.
            Defaults to 30 days before end_date.
        end_date: Last day to include, formatted YYYY-MM-DD. Defaults to today.

    Returns:
        A GetMetricHistoryResult with "points" (date/value pairs; days with
        no recorded value for this metric are simply absent).
    """
    try:
        start, end = resolve_range(start_date, end_date, default_days=30)
    except ValueError as exc:
        raise _tool_error(ERR_INVALID_RANGE, str(exc)) from exc
    series = _fetch_metric_series(metric, start, end)
    return GetMetricHistoryResult(
        metric=metric,
        range=DateRange(start_date=start.isoformat(), end_date=end.isoformat()),
        points=[MetricSeriesPoint(date=p.day.isoformat(), value=p.value) for p in series],
        evidence=Evidence(**build_evidence(series, start, end)),
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get metric baseline",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def get_baseline(metric: str, start_date: str | None = None, end_date: str | None = None) -> GetBaselineResult:
    """
    Compute "what's normal" for one metric over a window: mean, median,
    and standard deviation. This is the number every other analytics tool
    below measures against, so a wider window (60-90+ days) gives a more
    stable baseline than the 30-day default read_health_data uses.

    Use this tool when:
    - the user asks what's normal/typical/usual for one metric (e.g.
      "what's normal for my resting heart rate?"), with no particular
      day or direction of change in mind.

    Do not use this tool when:
    - the user asks whether a metric is going up/down over time -> use
      `calculate_metric_trend` instead.
    - the user asks whether specific days looked unusual -> use
      `detect_metric_anomalies` instead.
    - the user wants two ranges compared against each other -> use
      `compare_metric_periods` instead.

    Privacy note: this server and its SQLite file are entirely local, but
    the data returned by this tool becomes part of the conversation sent
    to whatever model the calling client is configured with. If that
    model runs in the cloud rather than on your machine, treat this the
    same as pasting the data into a chat with that provider.

    Args:
        metric: One of steps, sleep_hours, resting_heart_rate, weight_kg,
            workout_minutes, mood, water_ml, heart_rate, hrv_ms.
        start_date: First day to include, formatted YYYY-MM-DD.
            Defaults to 90 days before end_date.
        end_date: Last day to include, formatted YYYY-MM-DD. Defaults to today.

    Returns:
        A GetBaselineResult with "baseline" (mean/median/stdev/n) plus
        "claim" — the evidence behind it (claim.evidence: how much of the
        window it is based on; claim.profile: per-dimension quality) and
        claim.decision, which says how strongly the baseline may be stated.
        Report every entry in claim.decision.must_state (e.g. "based on only
        60% of days") rather than stating mean/median as if they were
        computed from a complete series. All baseline fields are null and n
        is 0 if the metric has no data in range — not an error, since
        "nothing logged yet" is an expected state.
    """
    try:
        start, end = resolve_range(start_date, end_date, default_days=90)
    except ValueError as exc:
        raise _tool_error(ERR_INVALID_RANGE, str(exc)) from exc
    series = _fetch_metric_series(metric, start, end)
    stats = _baseline_stats(series)
    evidence = Evidence(**build_evidence(series, start, end))
    assessment = assess_baseline(metric, series, start, end, effect=stats.model_dump())
    return GetBaselineResult(
        metric=metric,
        range=DateRange(start_date=start.isoformat(), end_date=end.isoformat()),
        baseline=stats,
        claim=_claim_evidence(assessment, evidence),
        evidence=evidence,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Detect metric anomalies",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def detect_metric_anomalies(
    metric: str, start_date: str | None = None, end_date: str | None = None, threshold: float = 3.5
) -> DetectAnomaliesResult:
    """
    Flag days where one metric deviated sharply from its own baseline over
    the window, using a median/MAD-based modified z-score rather than a
    mean/stdev z-score — more robust for short, noisy personal-health
    series, where the mean/stdev version is easily dragged around by the
    very outliers it's supposed to catch.

    Use this tool when:
    - the user asks whether anything looked unusual/off/weird on
      particular days for one metric (e.g. "was there anything unusual
      about my sleep last month?").

    Do not use this tool when:
    - the user wants a general sense of what's typical, with no interest
      in flagging specific days -> use `get_baseline` instead.
    - the user asks about a steady increase/decrease over time rather
      than isolated spikes/dips -> use `calculate_metric_trend` instead.
    - the user already has one specific date in mind and wants the full
      "why" behind it (baseline, anomaly, trend, and correlations
      together) -> use `explain_metric_change` instead.

    Privacy note: this server and its SQLite file are entirely local, but
    the data returned by this tool becomes part of the conversation sent
    to whatever model the calling client is configured with. If that
    model runs in the cloud rather than on your machine, treat this the
    same as pasting the data into a chat with that provider.

    Args:
        metric: One of steps, sleep_hours, resting_heart_rate, weight_kg,
            workout_minutes, mood, water_ml, heart_rate, hrv_ms.
        start_date: First day to include, formatted YYYY-MM-DD.
            Defaults to 90 days before end_date.
        end_date: Last day to include, formatted YYYY-MM-DD. Defaults to today.
        threshold: Modified z-score cutoff. 3.5 (the default, Iglewicz &
            Hoaglin's standard value) flags only clear outliers; lower it
            (e.g. 2.5) to see more borderline days.

    Returns:
        A DetectAnomaliesResult with "anomalies" (empty if fewer than 5
        days have data, or if the metric has no meaningful spread) plus
        "evidence" for the window they were computed over. An empty
        "anomalies" list with evidence.confidence "moderate" or "low"
        means "not enough data to tell," not "nothing unusual happened" —
        say that explicitly rather than reporting a clean bill of health.
    """
    try:
        start, end = resolve_range(start_date, end_date, default_days=90)
    except ValueError as exc:
        raise _tool_error(ERR_INVALID_RANGE, str(exc)) from exc
    series = _fetch_metric_series(metric, start, end)
    anomalies = detect_anomalies(series, threshold=threshold)
    assessment = assess_anomaly(
        metric, series, start, end, effect={"n_anomalies": len(anomalies), "threshold": threshold}
    )
    evidence = Evidence(**build_evidence(series, start, end))
    return DetectAnomaliesResult(
        metric=metric,
        range=DateRange(start_date=start.isoformat(), end_date=end.isoformat()),
        threshold=threshold,
        anomalies=[AnomalyPoint(**a) for a in anomalies],
        evidence=evidence,
        **_migrated_claim_fields(assessment, evidence),
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Calculate metric trend",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def calculate_metric_trend(
    metric: str, start_date: str | None = None, end_date: str | None = None
) -> CalculateTrendResult:
    """
    Fit a simple straight-line trend to one metric over a window and
    report its direction, slope (change per day), and r_squared (how well
    a straight line actually fits — low r_squared means "noisy," not
    "flat").

    Use this tool when:
    - the user asks whether one metric is trending up/down/flat over a
      continuous window (e.g. "is my weight trending down?", "how has my
      HRV changed?").

    Do not use this tool when:
    - the user wants two specific, separately-defined ranges compared
      (e.g. "this month vs last month") rather than a single continuous
      slope -> use `compare_metric_periods` instead.
    - the user asks what's typical/normal rather than which direction
      it's moving -> use `get_baseline` instead.
    - the user asks about isolated unusual days rather than an overall
      direction -> use `detect_metric_anomalies` instead.

    Privacy note: this server and its SQLite file are entirely local, but
    the data returned by this tool becomes part of the conversation sent
    to whatever model the calling client is configured with. If that
    model runs in the cloud rather than on your machine, treat this the
    same as pasting the data into a chat with that provider.

    Args:
        metric: One of steps, sleep_hours, resting_heart_rate, weight_kg,
            workout_minutes, mood, water_ml, heart_rate, hrv_ms.
        start_date: First day to include, formatted YYYY-MM-DD.
            Defaults to 30 days before end_date.
        end_date: Last day to include, formatted YYYY-MM-DD. Defaults to today.

    Returns:
        A CalculateTrendResult with "trend" plus "evidence" for the window
        it was fit over. direction is "insufficient_data" below 3 data
        points in range. Even with a direction and slope, a low
        evidence.coverage_ratio or "moderate"/"low" confidence means the
        line is fit through a sparse series — flag that when reporting the
        trend (e.g. "a decline, though the data only covers 60% of days")
        rather than stating the slope as a clean, complete measurement.
    """
    try:
        start, end = resolve_range(start_date, end_date, default_days=30)
    except ValueError as exc:
        raise _tool_error(ERR_INVALID_RANGE, str(exc)) from exc
    series = _fetch_metric_series(metric, start, end)
    trend = _trend_stats(series)
    assessment = assess_trend(metric, series, start, end, effect=trend.model_dump())
    evidence = Evidence(**build_evidence(series, start, end))
    return CalculateTrendResult(
        metric=metric,
        range=DateRange(start_date=start.isoformat(), end_date=end.isoformat()),
        trend=trend,
        evidence=evidence,
        **_migrated_claim_fields(assessment, evidence),
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Compare two periods",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def compare_metric_periods(
    metric: str,
    period_a_start: str,
    period_a_end: str,
    period_b_start: str,
    period_b_end: str,
) -> ComparePeriodsResult:
    """
    Compare one metric's average between two date ranges — e.g. "this
    month vs. last month" or "since starting a new medication vs. before."
    The two ranges may be any length and need not be adjacent or equal in
    size; each is summarized with its own baseline first.

    Use this tool when:
    - the user names or implies two distinct date ranges to weigh against
      each other (e.g. "compare my average steps this month to last
      month", "since starting a new medication vs. before").

    Do not use this tool when:
    - there's only one continuous window and the question is about
      direction over time, not two discrete ranges -> use
      `calculate_metric_trend` instead.
    - the user wants "what's normal" for a single window, not a
      before/after comparison -> use `get_baseline` instead.

    Privacy note: this server and its SQLite file are entirely local, but
    the data returned by this tool becomes part of the conversation sent
    to whatever model the calling client is configured with. If that
    model runs in the cloud rather than on your machine, treat this the
    same as pasting the data into a chat with that provider.

    Args:
        metric: One of steps, sleep_hours, resting_heart_rate, weight_kg,
            workout_minutes, mood, water_ml, heart_rate, hrv_ms.
        period_a_start, period_a_end: The "current"/later period, YYYY-MM-DD.
        period_b_start, period_b_end: The period it's compared against, YYYY-MM-DD.

    Returns:
        A ComparePeriodsResult with each period's own baseline stats, plus
        "delta" (period_a mean minus period_b mean) and "pct_change". Read
        claim.decision before reporting the comparison: its "tier"
        (insufficient, suggestive, detectable_not_meaningful, or supported)
        says how strongly it may be stated, and every entry in
        "must_state" has to be mentioned if you report it.
        claim.evidence_a/claim.evidence_b report each period's coverage
        separately — the two periods can have very different coverage
        (e.g. this month is 90% logged, last month only 40%), and that
        asymmetry matters more than either evidence object alone. Both
        delta and pct_change are null if either period has no data at all.
    """
    try:
        start_a, end_a = resolve_range(period_a_start, period_a_end, default_days=0)
        start_b, end_b = resolve_range(period_b_start, period_b_end, default_days=0)
    except ValueError as exc:
        raise _tool_error(ERR_INVALID_RANGE, str(exc)) from exc
    series_a = _fetch_metric_series(metric, start_a, end_a)
    series_b = _fetch_metric_series(metric, start_b, end_b)
    result = compare_periods(series_a, series_b)
    assessment = assess_window_comparison(
        metric,
        series_a,
        (start_a, end_a),
        series_b,
        (start_b, end_b),
        effect={"delta": result["delta"], "pct_change": result["pct_change"]},
    )
    claim = _claim_evidence_comparative(
        assessment,
        Evidence(**build_evidence(series_a, start_a, end_a)),
        Evidence(**build_evidence(series_b, start_b, end_b)),
    )
    return ComparePeriodsResult(
        metric=metric,
        period_a=DateRange(start_date=start_a.isoformat(), end_date=end_a.isoformat()),
        period_b=DateRange(start_date=start_b.isoformat(), end_date=end_b.isoformat()),
        period_a_stats=BaselineStats(**result["period_a"]),
        period_b_stats=BaselineStats(**result["period_b"]),
        delta=result["delta"],
        pct_change=result["pct_change"],
        claim=claim,
        period_a_evidence=claim.evidence_a,
        period_b_evidence=claim.evidence_b,
        evidence_profile=claim.profile,
        claim_decision=claim.decision,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Find correlation between two metrics",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def find_metric_correlation(
    metric_a: str,
    metric_b: str,
    start_date: str | None = None,
    end_date: str | None = None,
    lag_days: int = 0,
) -> CorrelationResult:
    """
    Compute the Pearson correlation between two metrics over the same
    window, joined by date. Correlation, not causation: a strong r just
    means the two moved together, not that one caused the other.

    Use this tool when:
    - the user names or implies two *different* metrics and asks whether
      they move together (e.g. "does my sleep affect my mood?", "did my
      HRV change after I increased my workouts?").

    Do not use this tool when:
    - only one metric is in question -> use `get_baseline`,
      `calculate_metric_trend`, or `detect_metric_anomalies` instead,
      depending on what's being asked about that one metric.

    Privacy note: this server and its SQLite file are entirely local, but
    the data returned by this tool becomes part of the conversation sent
    to whatever model the calling client is configured with. If that
    model runs in the cloud rather than on your machine, treat this the
    same as pasting the data into a chat with that provider.

    Args:
        metric_a, metric_b: Any two of steps, sleep_hours,
            resting_heart_rate, weight_kg, workout_minutes, mood, water_ml, heart_rate, hrv_ms.
        start_date: First day to include, formatted YYYY-MM-DD.
            Defaults to 90 days before end_date.
        end_date: Last day to include, formatted YYYY-MM-DD. Defaults to today.
        lag_days: Shift metric_b this many days later before joining — 1
            tests whether metric_a today predicts metric_b tomorrow (e.g.
            "does poor sleep tonight predict lower steps tomorrow?").
            0 (default) compares same-day values.

    Returns:
        A CorrelationResult with "r" (-1 to 1) and "n" (overlapping days
        used); "r" is null with fewer than 4 overlapping days. Read
        claim.decision before reporting "r": its "tier" (insufficient,
        suggestive, detectable_not_meaningful, or supported) says how
        strongly the correlation may be stated, and every entry in
        "must_state" has to be mentioned if you report it. A high "r"
        from a thin paired sample or a gappy series is reflected there,
        not in "r" itself — never judge the strength of a correlation
        from "r" alone.
    """
    try:
        start, end = resolve_range(start_date, end_date, default_days=90)
    except ValueError as exc:
        raise _tool_error(ERR_INVALID_RANGE, str(exc)) from exc
    series_a = _fetch_metric_series(metric_a, start, end)
    series_b = _fetch_metric_series(metric_b, start, end)
    result = find_correlations(series_a, series_b, lag_days=lag_days)
    assessment = assess_correlation(
        metric_a,
        series_a,
        metric_b,
        series_b,
        start,
        end,
        lag_days=lag_days,
        effect={"r": result["r"], "n": result["n"], "lag_days": lag_days},
    )
    return CorrelationResult(
        metric_a=metric_a,
        metric_b=metric_b,
        **result,
        **_migrated_claim_fields_comparative(
            assessment,
            Evidence(**build_evidence(series_a, start, end)),
            Evidence(**build_evidence(series_b, start, end)),
        ),
    )


# ---------------------------------------------------------------------------
# Layer 3: Personal intelligence
# ---------------------------------------------------------------------------
#
# These compose the Layer-2 functions above rather than calling any LLM
# themselves — each returns structured facts (numbers, flags, short plain
# strings), never generated prose. Turning those facts into a narrative
# answer is left to whichever model is calling this server: an MCP tool
# that phoned out to an LLM internally would double the latency and cost of
# every call, and would need its own API key/network access, undermining
# the fully-local, single-model design the rest of this server relies on.
# get_health_summary, get_behavior_patterns, and get_personal_baseline
# (percentile-flavored framing of get_baseline) are natural next additions
# in this same style: fetch via _fetch_metric_series, compute via
# analytics.py, return facts.


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get recent changes",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def get_recent_changes(days: int = 7) -> GetRecentChangesResult:
    """
    Scan every (non-private) metric for what's changed lately: a recent
    period vs. the four-times-as-long period before it (period-over-period
    shift), any anomalies inside the recent period, and a trend over it.
    The single best tool to start a "how have I been doing?" conversation
    with — it does the scanning across all metrics that would otherwise
    take one get_baseline/detect_metric_anomalies/calculate_metric_trend
    call per metric.

    Do not use this tool when:
    - the user already named a specific metric and wants the full
      why-bundle for it (value, baseline, anomaly flag, trend, correlated
      metrics) -> use `explain_metric_change` instead.

    Privacy note: this server and its SQLite file are entirely local, but
    the data returned by this tool becomes part of the conversation sent
    to whatever model the calling client is configured with. If that
    model runs in the cloud rather than on your machine, treat this the
    same as pasting the data into a chat with that provider.

    Args:
        days: Length of the "recent" window in days (default 7). The
            comparison baseline is the 4x-as-long period immediately
            before it, so a 7-day recent window compares against the
            preceding 28 days.

    Returns:
        A GetRecentChangesResult with "changes": one entry per metric per
        notable finding (a >=15% period-over-period shift, any anomaly in
        the recent window, or a clear non-flat trend with r_squared >=
        0.3). Metrics with nothing notable, or no data, are simply absent
        — this tool reports signal, not a status page for every metric.
        Each change carries its own "evidence" (coverage over the baseline
        + recent window it was computed from) — do not repeat a change's
        detail to the user as a plain fact when its evidence.confidence is
        "moderate" or "low"; say the finding is based on partial data
        (e.g. name the coverage_ratio or recent_gap_days) instead of
        stating it outright.
    """
    if days < 2:
        raise _tool_error(ERR_INVALID_RANGE, "days must be at least 2.")
    end = date_type.today()
    recent_start = end - timedelta(days=days)
    baseline_start = recent_start - timedelta(days=days * 4)
    baseline_end = recent_start - timedelta(days=1)

    changes: list[ChangeNote] = []
    for metric in METRIC_COLUMNS:
        if metric in PRIVATE_FIELDS:
            continue
        recent_series = _fetch_metric_series(metric, recent_start, end)
        baseline_series = _fetch_metric_series(metric, baseline_start, baseline_end)
        # One evidence object per metric, over the full window this metric's
        # notes below are drawn from (baseline period + recent period), so a
        # "shift"/"anomaly"/"trend" note is never reported without the
        # coverage it's based on — a >=15% shift built on a mostly-empty
        # baseline period is exactly the kind of false-confidence claim this
        # tool exists to avoid.
        metric_evidence = Evidence(**build_evidence(baseline_series + recent_series, baseline_start, end))

        comparison = compare_periods(recent_series, baseline_series)
        if comparison["pct_change"] is not None and abs(comparison["pct_change"]) >= 15:
            direction = "up" if comparison["pct_change"] > 0 else "down"
            shift_claim = assess_window_comparison(
                metric,
                recent_series,
                (recent_start, end),
                baseline_series,
                (baseline_start, baseline_end),
                effect={"delta": comparison["delta"], "pct_change": comparison["pct_change"]},
            )
            changes.append(
                ChangeNote(
                    metric=metric,
                    kind="shift",
                    detail=(
                        f"{metric} is {direction} {abs(comparison['pct_change'])}% over the last {days} days "
                        f"(avg {comparison['period_a']['mean']}) vs. the {days * 4} days before that "
                        f"(avg {comparison['period_b']['mean']})."
                    ),
                    evidence=metric_evidence,
                    **_migrated_claim_fields(shift_claim, metric_evidence),
                )
            )

        recent_anomalies = detect_anomalies(recent_series)
        anomaly_claim = (
            assess_anomaly(metric, recent_series, recent_start, end, effect={"n_anomalies": len(recent_anomalies)})
            if recent_anomalies
            else None
        )
        for anomaly in recent_anomalies:
            changes.append(
                ChangeNote(
                    metric=metric,
                    kind="anomaly",
                    detail=(
                        f"{metric} on {anomaly['date']} was {anomaly['value']} "
                        f"({anomaly['direction']} the recent median, "
                        f"modified z-score {anomaly['modified_z_score']})."
                    ),
                    evidence=metric_evidence,
                    **_migrated_claim_fields(anomaly_claim, metric_evidence),
                )
            )

        trend = calculate_trend(recent_series)
        if trend["direction"] not in ("flat", "insufficient_data") and (trend["r_squared"] or 0) >= 0.3:
            trend_claim = assess_trend(metric, recent_series, recent_start, end, effect=dict(trend))
            changes.append(
                ChangeNote(
                    metric=metric,
                    kind="trend",
                    detail=f"{metric} has been {trend['direction']} over the last {days} days "
                    f"({trend['slope_per_day']:+g}/day, r²={trend['r_squared']}).",
                    evidence=metric_evidence,
                    **_migrated_claim_fields(trend_claim, metric_evidence),
                )
            )

    return GetRecentChangesResult(
        recent_range=DateRange(start_date=recent_start.isoformat(), end_date=end.isoformat()),
        baseline_range=DateRange(start_date=baseline_start.isoformat(), end_date=baseline_end.isoformat()),
        changes=changes,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Explain a metric change",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def explain_metric_change(metric: str, date: str) -> ExplainMetricChangeResult:
    """
    Build an evidence bundle for "why did my <metric> look like that on
    <date>?": that day's value against a 90-day baseline, whether it
    qualifies as an anomaly, the trend leading into it, and any other
    metric that correlates with it strongly enough to be worth mentioning.
    Returns facts, not an explanation — turning "sleep was 2.6 stdev below
    baseline and resting heart rate correlates at r=0.71" into an actual
    answer for the person is what the calling model should do with these
    facts, not something this tool guesses at itself.

    Do not use this tool when:
    - scanning across many metrics for what changed lately, without a
      specific metric/date in mind -> use `get_recent_changes` instead.
    - you only need one piece of this bundle (just the baseline, just the
      trend, just anomalies, or just a correlation) rather than the full
      why-explanation -> use `get_baseline`, `calculate_metric_trend`,
      `detect_metric_anomalies`, or `find_metric_correlation` directly
      instead.

    Privacy note: this server and its SQLite file are entirely local, but
    the data returned by this tool becomes part of the conversation sent
    to whatever model the calling client is configured with. If that
    model runs in the cloud rather than on your machine, treat this the
    same as pasting the data into a chat with that provider.

    Args:
        metric: One of steps, sleep_hours, resting_heart_rate, weight_kg,
            workout_minutes, mood, water_ml, heart_rate, hrv_ms.
        date: The day to explain, formatted YYYY-MM-DD.

    Returns:
        An ExplainMetricChangeResult with the day's value, the 90-day
        baseline it's measured against, whether it's an anomaly, the
        30-day trend ending on that date, and up to 5 other metrics with
        |r| >= 0.5 over the same 90-day window (each still just a
        correlation — see find_metric_correlation's note on causation).
        "headline_claim.evidence" and "trend_claim.evidence" report coverage for the
        90-day and 30-day windows respectively; each correlated metric
        carries its own pair too. "narrative_facts" restates the above as
        short plain-English sentences but does NOT itself hedge on
        coverage. Use "overall_decision" for that: it is the single
        weakest-of-N decision across the headline claim, the trend, and
        every surfaced correlation, so its tier and must_state are what
        to report/caveat with — not something to re-derive yourself from
        the individual evidence/confidence fields.
    """
    try:
        target_day = parse_date(date, "date")
    except ValueError as exc:
        raise _tool_error(ERR_INVALID_DATE, str(exc)) from exc

    baseline_start = target_day - timedelta(days=90)
    series = _fetch_metric_series(metric, baseline_start, target_day)
    stats = compute_baseline(series)
    target_point = next((p for p in series if p.day == target_day), None)
    value = target_point.value if target_point else None

    anomalies = detect_anomalies(series)
    matching_anomaly = next((a for a in anomalies if a["date"] == target_day.isoformat()), None)

    trend_start = target_day - timedelta(days=30)
    trend_series = _fetch_metric_series(metric, trend_start, target_day)
    trend = calculate_trend(trend_series)

    conflicting_days = 0
    try:
        with _readonly_connection(HEALTH_DB_PATH) as conn:
            conflicting_days = count_source_conflicts(conn, metric, trend_start, target_day)
    except db_error_types() as exc:
        logger.error("Database error counting source conflicts for %s: %s", metric, exc)

    correlated: list[CorrelationResult] = []
    # Paired with `correlated` positionally (before the top-5 truncation below) so the composite
    # decision below can be built from exactly the correlations actually surfaced to the caller.
    correlation_assessments: list[Assessment] = []
    for other in METRIC_COLUMNS:
        if other == metric or other in PRIVATE_FIELDS:
            continue
        other_series = _fetch_metric_series(other, baseline_start, target_day)
        result = find_correlations(series, other_series, lag_days=0)
        if result["r"] is not None and abs(result["r"]) >= 0.5:
            corr_assessment = assess_correlation(
                metric,
                series,
                other,
                other_series,
                baseline_start,
                target_day,
                effect={"r": result["r"], "n": result["n"], "lag_days": 0},
            )
            correlated.append(
                CorrelationResult(
                    metric_a=metric,
                    metric_b=other,
                    **result,
                    **_migrated_claim_fields_comparative(
                        corr_assessment,
                        Evidence(**build_evidence(series, baseline_start, target_day)),
                        Evidence(**build_evidence(other_series, baseline_start, target_day)),
                    ),
                )
            )
            correlation_assessments.append(corr_assessment)
    ranked = sorted(
        zip(correlated, correlation_assessments, strict=True), key=lambda pair: abs(pair[0].r or 0), reverse=True
    )[:5]
    correlated = [c for c, _ in ranked]
    correlation_assessments = [a for _, a in ranked]

    facts: list[str] = []
    if value is None:
        facts.append(f"No {metric} value is logged for {date}.")
    else:
        facts.append(f"{metric} on {date} was {value}.")
    if stats["mean"] is not None:
        facts.append(
            f"Over the preceding 90 days, {metric} averaged {stats['mean']} "
            f"(median {stats['median']}, n={stats['n']})."
        )
    if matching_anomaly:
        facts.append(
            f"That value is a statistical anomaly: {matching_anomaly['direction']} the 90-day median "
            f"(modified z-score {matching_anomaly['modified_z_score']})."
        )
    if trend["direction"] not in ("flat", "insufficient_data"):
        facts.append(
            f"{metric} had been {trend['direction']} over the 30 days leading up to {date} "
            f"(r²={trend['r_squared']})."
        )
    for c in correlated:
        facts.append(f"{c.metric_b} correlates with {metric} over this window (r={c.r}, n={c.n}).")

    if conflicting_days:
        window_days = (target_day - trend_start).days + 1
        facts.append(
            f"{metric} had conflicting values from different sources on {conflicting_days} of the "
            f"{window_days} days in the trend window above, so that trend is less certain than it looks."
        )

    sessions: list[WorkoutSessionRow] = []
    if metric == "workout_minutes":
        try:
            conn = connect_writable(HEALTH_DB_PATH)
            try:
                ensure_schema(conn)
                conn.row_factory = row_class()
                session_rows = query_workout_sessions(conn, start=date, end=date)
            finally:
                conn.close()
        except db_error_types() as exc:
            logger.error("Database error reading from %s: %s", HEALTH_DB_PATH, exc)
        else:
            sessions = [WorkoutSessionRow(**row) for row in session_rows]
            for s in sessions:
                detail = f"Logged workout: {s.activity_type}, {s.duration_minutes} min"
                if s.intensity:
                    detail += f", {s.intensity} intensity"
                if s.avg_heart_rate:
                    detail += f", avg HR {s.avg_heart_rate}"
                facts.append(detail + ".")

    anomaly_assessment = assess_anomaly(
        metric,
        series,
        baseline_start,
        target_day,
        effect={
            "is_anomaly": matching_anomaly is not None,
            "modified_z_score": matching_anomaly["modified_z_score"] if matching_anomaly else None,
        },
    )
    trend_assessment = assess_trend(metric, trend_series, trend_start, target_day, effect=dict(trend))
    overall_decision = combine_decisions(
        [anomaly_assessment.decision, trend_assessment.decision, *(a.decision for a in correlation_assessments)]
    )
    headline_claim = _claim_evidence(
        anomaly_assessment, Evidence(**build_evidence(series, baseline_start, target_day))
    )
    trend_claim = _claim_evidence(
        trend_assessment, Evidence(**build_evidence(trend_series, trend_start, target_day))
    )
    return ExplainMetricChangeResult(
        metric=metric,
        date=date,
        value=value,
        baseline_range=DateRange(start_date=baseline_start.isoformat(), end_date=target_day.isoformat()),
        baseline=BaselineStats(**stats),
        is_anomaly=matching_anomaly is not None,
        modified_z_score=matching_anomaly["modified_z_score"] if matching_anomaly else None,
        trend=_trend_stats(trend_series),
        correlated_metrics=correlated,
        sessions=sessions,
        narrative_facts=facts,
        headline_claim=headline_claim,
        trend_claim=trend_claim,
        evidence_profile=headline_claim.profile,
        claim_decision=headline_claim.decision,
        trend_evidence_profile=trend_claim.profile,
        trend_claim_decision=trend_claim.decision,
        conflicting_days=conflicting_days,
        overall_decision=ClaimDecisionOut(**overall_decision.to_mcp()),
    )


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------
#
# Unlike the tools above, resources are read-only, side-effect-free, and
# addressed by URI rather than invoked with arguments — the right shape for
# reference data a client might want to read once and cache (the metric
# schema) or fetch directly by a known key (a specific day), rather than
# something that needs a tool call's request/response semantics.


@mcp.resource(
    "health://metrics/schema",
    name="Metric schema",
    description=(
        "The full set of metrics this server tracks, with each one's valid "
        "range (see logic.METRIC_BOUNDS) and whether it's currently "
        "configured as private (see HEALTH_PRIVATE_FIELDS). Read this to "
        "see accepted ranges up front, instead of discovering them one at "
        "a time from log_daily_metric's invalid_metric_value errors."
    ),
    mime_type="application/json",
)
def metrics_schema() -> list[dict]:
    return [
        MetricDefinition(name=name, min=low, max=high, label=label, private=name in PRIVATE_FIELDS).model_dump()
        for name, (low, high, label) in METRIC_BOUNDS.items()
    ]


@mcp.resource(
    "health://day/{date}",
    name="Single day snapshot",
    description=(
        "Read-only snapshot of one day's metrics, addressed directly by "
        "date (YYYY-MM-DD) instead of a tool call. Equivalent to "
        "read_health_data with start_date == end_date == date, including "
        "the same field-level privacy redaction — a date with no data at "
        "all still resolves, just with every field null, rather than "
        "erroring."
    ),
    mime_type="application/json",
)
def day_snapshot(date: str) -> dict:
    try:
        day = parse_date(date, "date")
    except ValueError as exc:
        raise ResourceError(str(exc)) from exc

    try:
        with _readonly_connection(HEALTH_DB_PATH) as conn:
            rows = daily_metrics_wide(conn, METRIC_COLUMNS, day.isoformat(), day.isoformat())
            row = rows[0] if rows else None
    except db_error_types() as exc:
        logger.error("Database error reading %s: %s", HEALTH_DB_PATH, exc)
        raise ResourceError(f"Could not read the health database: {exc}") from exc

    if row is None:
        return DailyMetricsRow(date=day.isoformat()).model_dump()
    return DailyMetricsRow(**_redact_private_fields(row)).model_dump()


def main():
    # Cloud platforms (e.g. Manufact) set PORT and expect an HTTP server
    # listening on it; Claude Desktop and other local stdio clients don't
    # set PORT at all, in which case we fall back to stdio as before.
    port = os.environ.get("PORT")
    if port is not None:
        mcp.run(transport="http", host="0.0.0.0", port=int(port))
    else:
        mcp.run()


if __name__ == "__main__":
    main()
