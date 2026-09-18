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

import csv
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
from fastmcp.exceptions import ResourceError, ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from analytics import (
    Point,
    calculate_trend,
    compare_periods,
    detect_anomalies,
    find_correlations,
)
from analytics import baseline as compute_baseline
from evidence import build_coverage_summary, build_evidence
from logic import (
    MAX_ROWS_RETURNED,
    METRIC_BOUNDS,
    WORKOUT_INTENSITIES,
    aggregate_measurements_to_daily,
    clear_daily_metric,
    connect_writable,
    count_source_conflicts,
    daily_metrics_wide,
    db_error_types,
    default_data_dir,
    ensure_schema,
    insert_measurement,
    insert_workout_session,
    numeric_stats,
    parse_date,
    query_measurements,
    query_workout_sessions,
    resolve_range,
    row_class,
    upsert_daily_metric_measurements,
    validate_metrics,
)
from logic import (
    get_metric_provenance as _get_metric_provenance,
)
from logic import (
    readonly_connection as _logic_readonly_connection,
)

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
# reported — the single place to touch when another metric is added.
METRIC_COLUMNS = [
    "steps",
    "sleep_hours",
    "resting_heart_rate",
    "weight_kg",
    "workout_minutes",
    "mood",
    "water_ml",
    "heart_rate",
    "hrv_ms",
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
    heart_rate: int | None = None
    hrv_ms: float | None = None


class DateRange(BaseModel):
    start_date: str
    end_date: str


class Gap(BaseModel):
    start: str
    end: str
    days: int


class Evidence(BaseModel):
    """Coverage/quality of the data a single-metric analytical result is
    based on. See evidence.build_evidence for how each field is computed.
"""

    requested_start: str
    requested_end: str
    observed_start: str | None = None
    observed_end: str | None = None
    expected_days: int
    observed_days: int
    coverage_ratio: float
    missing_days: int
    measurement_count: int
    gaps: list[Gap]
    freshness_days: int | None = None
    recent_gap_days: int
    confidence: str


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
    heart_rate: MetricStats
    hrv_ms: MetricStats


class CoverageSummary(BaseModel):
    """Multi-metric coverage for a Layer-1 result spanning several metrics
    at once. See evidence.build_coverage_summary for how each field is
    computed; unlike Evidence (one metric, gaps/freshness/recent_gap),
    this reports a coverage_percent per metric side by side.
    """

    period: str
    days_expected: int
    days_with_data: int
    coverage_percent: float
    missing_days: int
    metrics: dict[str, float]
    confidence: str


class ReadHealthDataResult(BaseModel):
    range: DateRange
    rows: list[DailyMetricsRow]
    truncated: bool
    summary: HealthDataSummary
    coverage: CoverageSummary


class ExportCsvResult(BaseModel):
    path: str
    rows_exported: int
    range: DateRange


class LogDailyMetricResult(BaseModel):
    logged: dict[str, int | float]
    row: DailyMetricsRow


class ClearMetricResult(BaseModel):
    cleared: str
    row: DailyMetricsRow | None = None
    note: str | None = None


class MeasurementRow(BaseModel):
    id: int
    timestamp: str
    metric: str
    value: float
    unit: str | None = None
    source: str | None = None
    source_type: str | None = None
    created_at: str


class LogMeasurementResult(BaseModel):
    measurement: MeasurementRow


class ReadMeasurementsResult(BaseModel):
    measurements: list[MeasurementRow]
    count: int


class AggregateMeasurementsResult(BaseModel):
    date: str
    aggregated: dict[str, float]
    row: DailyMetricsRow


class SourceBreakdown(BaseModel):
    source: str | None = None
    value: float
    n: int
    latest_timestamp: str


class GetMetricProvenanceResult(BaseModel):
    metric: str
    date: str
    sources: list[SourceBreakdown]
    conflict: bool


# --- Layer 2 (analytics) / Layer 3 (personal intelligence) output models --
#
# These wrap the pure functions in analytics.py the same way the models
# above wrap logic.py: FastMCP derives a JSON schema from the return type,
# so a client gets structured_content it can consume directly rather than
# re-parsing a free-form string.


class MetricSeriesPoint(BaseModel):
    date: str
    value: float


class GetMetricHistoryResult(BaseModel):
    metric: str
    range: DateRange
    points: list[MetricSeriesPoint]
    evidence: Evidence


class BaselineStats(BaseModel):
    mean: float | None = None
    median: float | None = None
    stdev: float | None = None
    n: int


class GetBaselineResult(BaseModel):
    metric: str
    range: DateRange
    baseline: BaselineStats
    evidence: Evidence


class AnomalyPoint(BaseModel):
    date: str
    value: float
    modified_z_score: float
    direction: str


class DetectAnomaliesResult(BaseModel):
    metric: str
    range: DateRange
    threshold: float
    anomalies: list[AnomalyPoint]
    evidence: Evidence


class TrendStats(BaseModel):
    direction: str
    slope_per_day: float | None = None
    r_squared: float | None = None
    n: int
    span_days: int | None = None


class CalculateTrendResult(BaseModel):
    metric: str
    range: DateRange
    trend: TrendStats
    evidence: Evidence


class ComparePeriodsResult(BaseModel):
    metric: str
    period_a: DateRange
    period_b: DateRange
    period_a_stats: BaselineStats
    period_b_stats: BaselineStats
    delta: float | None = None
    pct_change: float | None = None
    period_a_evidence: Evidence
    period_b_evidence: Evidence


class CorrelationResult(BaseModel):
    metric_a: str
    metric_b: str
    lag_days: int
    r: float | None = None
    n: int
    sample_confidence: str
    note: str | None = None
    evidence_a: Evidence | None = None
    evidence_b: Evidence | None = None


class ChangeNote(BaseModel):
    metric: str
    kind: str  # "shift" (period-over-period) | "anomaly" | "trend"
    detail: str
    evidence: Evidence


class GetRecentChangesResult(BaseModel):
    recent_range: DateRange
    baseline_range: DateRange
    changes: list[ChangeNote]


class WorkoutSessionRow(BaseModel):
    id: int
    date: str
    activity_type: str
    start_time: str | None = None
    duration_minutes: int
    intensity: str | None = None
    avg_heart_rate: int | None = None
    max_heart_rate: int | None = None
    source: str | None = None
    notes: str | None = None
    created_at: str


class LogWorkoutSessionResult(BaseModel):
    session: WorkoutSessionRow


class ReadWorkoutSessionsResult(BaseModel):
    sessions: list[WorkoutSessionRow]
    count: int


class ExplainMetricChangeResult(BaseModel):
    metric: str
    date: str
    value: float | None = None
    baseline_range: DateRange
    baseline: BaselineStats
    is_anomaly: bool
    modified_z_score: float | None = None
    trend: TrendStats
    correlated_metrics: list[CorrelationResult]
    sessions: list[WorkoutSessionRow] = []
    narrative_facts: list[str]
    baseline_evidence: Evidence
    trend_evidence: Evidence
    conflicting_days: int = 0


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

# Field-level privacy: metrics listed here (comma-separated) are never
# exposed by any tool, no matter how they're stored — read_health_data
# always reports them as null (in both "rows" and "summary"), and the
# "row" echoed back by log_daily_metric/clear_metric redacts them too, so
# even the write tools' own responses can't leak a value back to the
# model. The actual value is still written to and kept in the database
# (so e.g. weight_kg can still be logged for your own records), just never
# read back through the MCP tools. Unknown names are logged and ignored
# rather than crashing the server, since a typo in this config shouldn't
# take down the whole thing.
def _parse_private_fields(raw: str) -> frozenset[str]:
    names = {name.strip() for name in raw.split(",") if name.strip()}
    unknown = names - set(METRIC_COLUMNS)
    if unknown:
        logger.warning(
            "HEALTH_PRIVATE_FIELDS contains unknown field(s) %s; ignoring. Valid fields: %s",
            sorted(unknown),
            ", ".join(METRIC_COLUMNS),
        )
    return frozenset(names & set(METRIC_COLUMNS))


PRIVATE_FIELDS = _parse_private_fields(os.environ.get("HEALTH_PRIVATE_FIELDS", ""))


# daily_metrics.value is stored as REAL regardless of a metric's logical
# type (see db/schema.sql), so a "mean"-method metric can come back with a
# genuine fractional part -- e.g. two resting_heart_rate readings of 62
# and 67 average to 64.5 -- that DailyMetricsRow's `int` fields would
# otherwise reject outright rather than silently truncate. Listed
# explicitly (matching METRIC_COLUMNS/METRIC_BOUNDS' style elsewhere in
# this file) rather than introspected from the pydantic model, since only
# these are declared `int` there; sleep_hours/weight_kg/hrv_ms are `float`
# and never need this.
INT_METRIC_COLUMNS = frozenset({"steps", "resting_heart_rate", "workout_minutes", "mood", "water_ml", "heart_rate"})


def _redact_private_fields(row: dict) -> dict:
    """Return a copy of a daily_metrics row dict with any private field
    forced to None, and any INT_METRIC_COLUMNS value rounded to the
    nearest int (see INT_METRIC_COLUMNS) -- both regardless of what's
    actually stored for it. Every call site that builds a DailyMetricsRow
    from a daily_metrics_wide row goes through this first.
    """
    return {
        k: (None if k in PRIVATE_FIELDS else round(v) if k in INT_METRIC_COLUMNS and v is not None else v)
        for k, v in row.items()
    }


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
ERR_INVALID_TIMESTAMP = "invalid_timestamp"
ERR_INVALID_METRIC = "invalid_metric"
ERR_INVALID_ACTIVITY_TYPE = "invalid_activity_type"
ERR_INVALID_DURATION = "invalid_duration"
ERR_INVALID_INTENSITY = "invalid_intensity"


def _tool_error(code: str, message: str) -> ToolError:
    return ToolError(f"[{code}] {message}")


def _is_locked_error(exc: Exception) -> bool:
    """True if exc looks like a lock/busy contention error rather than a
    missing/corrupt database — used to pick database_locked vs
    database_error so the two failure modes (retry-worthy vs not) are
    distinguishable by code, not just by re-reading the message text.

    Checks the exception's class *name* rather than isinstance against
    sqlite3.OperationalError specifically, since sqlcipher3's own
    OperationalError (used when HEALTH_DB_PASSPHRASE is set — see
    logic.db_error_types) is a separate class, not a subclass of
    sqlite3's, and this needs to recognize either.
    """
    return type(exc).__name__ == "OperationalError" and "lock" in str(exc).lower()


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

    Use this tool when:
    - the request is broad/general, across multiple metrics at once (e.g.
      "what health data do I have?", "overview of this week").

    Do not use this tool when:
    - the user wants one specific metric's history/trend over time -> use
      `get_metric_history` instead.
    - the user wants raw/individual measurement rows (timestamp, source) ->
      use `read_measurements` instead.
    - the user wants workout sessions specifically -> use
      `read_workout_sessions` instead.
    - the user is asking *why* something changed, or wants a trend,
      anomaly, comparison, or correlation -> use `explain_metric_change`
      (one metric, one date) or `get_recent_changes` (scan across all
      metrics) instead.

    Privacy note: this server and its SQLite file are entirely local, but
    the data returned by this tool becomes part of the conversation sent
    to whatever model the calling client is configured with. If that
    model runs in the cloud rather than on your machine, treat this the
    same as pasting the data into a chat with that provider.

    Args:
        start_date: First day to include, formatted YYYY-MM-DD.
            Defaults to 30 days before end_date. Ranges over ~10 years are rejected.
        end_date: Last day to include, formatted YYYY-MM-DD. Defaults to today.

    Returns:
        A ReadHealthDataResult with:
        - "range": the start/end dates actually used
        - "rows": one entry per day that has at least one recorded metric
          (date plus whichever of steps, sleep_hours, resting_heart_rate,
          weight_kg, workout_minutes, mood, water_ml, heart_rate, hrv_ms
          were logged for that
          day — fields with no data are null, not absent). Days with no
          data at all are simply absent from "rows". Capped at the most
          recent 400 matching days; see "truncated".
        - "truncated": true if more matching days existed than were returned in "rows"
        - "summary": days_with_data plus avg/min/max for each metric, computed
          over *all* matching days even when "rows" is truncated
        - "coverage": how complete this range's data actually is — the
          requested period, days_expected vs. days_with_data, an overall
          coverage_percent, and a per-metric coverage_percent in "metrics"
          (e.g. sleep_hours might be 90% logged while hrv_ms is only 40%).
          Use this before characterizing the data as a full picture: a
          "confidence" of "moderate" or "low" (or any one metric's percent
          being much lower than the others) means say so — e.g. "hrv_ms is
          only logged on 40% of these days, so treat any pattern there
          cautiously" — rather than treating every metric in "summary" as
          equally well-observed.

    Any metric listed in the HEALTH_PRIVATE_FIELDS environment variable is
    always reported as null here (in both "rows" and "summary") and is left
    out of "coverage.metrics" entirely, regardless of what's actually
    stored for it.
    """
    try:
        start, end = resolve_range(start_date, end_date, default_days=30)
    except ValueError as exc:
        raise _tool_error(ERR_INVALID_RANGE, str(exc)) from exc

    try:
        with _readonly_connection(HEALTH_DB_PATH) as conn:
            rows = daily_metrics_wide(conn, METRIC_COLUMNS, start.isoformat(), end.isoformat())
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

    truncated = len(rows) > MAX_ROWS_RETURNED
    returned_rows = rows[-MAX_ROWS_RETURNED:] if truncated else rows

    public_metrics = [m for m in METRIC_COLUMNS if m not in PRIVATE_FIELDS]
    return ReadHealthDataResult(
        range=DateRange(start_date=start.isoformat(), end_date=end.isoformat()),
        rows=[DailyMetricsRow(**_redact_private_fields(row)) for row in returned_rows],
        truncated=truncated,
        summary=HealthDataSummary(
            days_with_data=len(rows),
            **{
                metric: (MetricStats() if metric in PRIVATE_FIELDS else MetricStats(**numeric_stats(rows, metric)))
                for metric in METRIC_COLUMNS
            },
        ),
        coverage=CoverageSummary(**build_coverage_summary(rows, start, end, public_metrics)),
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Export health data to CSV",
        readOnlyHint=True,  # opened via _readonly_connection; cannot write the database
        idempotentHint=False,  # writes a new file each call
        openWorldHint=False,  # only ever touches the local SQLite file and local disk
    )
)
def export_health_data_csv(start_date: str | None = None, end_date: str | None = None) -> ExportCsvResult:
    """
    Write daily health metrics for a date range to a CSV file on disk,
    next to the database, instead of returning every row through this
    tool's own result.

    Privacy note: this server and its SQLite file are entirely local, but
    the data returned by this tool becomes part of the conversation sent
    to whatever model the calling client is configured with. If that
    model runs in the cloud rather than on your machine, treat this the
    same as pasting the data into a chat with that provider.

    Unlike read_health_data, this is not capped at MAX_ROWS_RETURNED and
    the row values themselves are not included in this tool's response —
    only the resulting file's path and a row count are. That means a
    long-range export doesn't have to pass through a cloud LLM's context
    just to produce a file you can open yourself (in a spreadsheet, a
    notebook, another tool, etc.). Any metric listed in
    HEALTH_PRIVATE_FIELDS is still written as an empty cell in the file,
    since those fields shouldn't leave the database at all, not just stay
    out of the model's context.

    Args:
        start_date: First day to include, formatted YYYY-MM-DD.
            Defaults to 30 days before end_date. Ranges over ~10 years are rejected.
        end_date: Last day to include, formatted YYYY-MM-DD. Defaults to today.

    Returns:
        An ExportCsvResult with "path" (the written file's absolute
        path), "rows_exported" (days with at least one recorded metric —
        days with no data at all are not written), and "range" (the
        start/end dates actually used).
    """
    try:
        start, end = resolve_range(start_date, end_date, default_days=30)
    except ValueError as exc:
        raise _tool_error(ERR_INVALID_RANGE, str(exc)) from exc

    try:
        with _readonly_connection(HEALTH_DB_PATH) as conn:
            rows = daily_metrics_wide(conn, METRIC_COLUMNS, start.isoformat(), end.isoformat())
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

    export_dir = HEALTH_DB_PATH.parent / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    out_path = export_dir / f"health_export_{start.isoformat()}_to_{end.isoformat()}.csv"

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["date", *METRIC_COLUMNS])
        for row in rows:
            # Routed through DailyMetricsRow (not just _redact_private_fields)
            # so int-typed metrics come out as ints, not the float SQLite's
            # REAL-typed daily_metrics.value column hands back — the same
            # coercion read_health_data/log_daily_metric already get for
            # free by constructing a DailyMetricsRow.
            typed = DailyMetricsRow(**_redact_private_fields(row)).model_dump()
            writer.writerow([typed["date"], *(typed[col] for col in METRIC_COLUMNS)])

    return ExportCsvResult(
        path=str(out_path.resolve()),
        rows_exported=len(rows),
        range=DateRange(start_date=start.isoformat(), end_date=end.isoformat()),
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
    heart_rate: int | None = None,
    hrv_ms: float | None = None,
) -> LogDailyMetricResult:
    """
    Record one or more health metrics for a single day, creating that
    day's row if it doesn't already have one.

    Privacy note: this server and its SQLite file are entirely local, but
    the data returned by this tool becomes part of the conversation sent
    to whatever model the calling client is configured with. If that
    model runs in the cloud rather than on your machine, treat this the
    same as pasting the data into a chat with that provider.

    Only the metrics you pass are written — anything left as null is not
    touched, so logging just today's mood doesn't erase today's steps if
    they were set earlier. To undo a value logged by mistake, use
    clear_metric rather than trying to overwrite it with a placeholder.

    Use this tool when:
    - the user is recording a simple day-level value for one of the nine
      fixed metrics below (e.g. "log my weight as 82 kg", "I walked 8,000
      steps today").

    Do not use this tool when:
    - the observation needs its own timestamp/source, or the day may have
      more than one reading of the same metric -> use `log_measurement` instead.
    - it's a workout/exercise session -> use `log_workout_session` instead
      (workout_minutes here is just the daily total, not the session itself).

    Args:
        date: The day to log, formatted YYYY-MM-DD.
        steps: Step count for the day. 0-200,000.
        sleep_hours: Hours of sleep. 0-24.
        resting_heart_rate: Resting heart rate in bpm. 20-250.
        weight_kg: Body weight in kilograms. 1-500.
        workout_minutes: Minutes of exercise. 0-1,440.
        mood: Mood rating on a 1-10 scale.
        water_ml: Water intake in millilitres. 0-10,000.
        heart_rate: Non-resting heart rate reading in bpm. 20-250.
        hrv_ms: Heart rate variability in milliseconds. 0-300.

    Returns:
        A LogDailyMetricResult with "logged" (just the fields this call set)
        and "row" (the day's full current state across all metrics,
        including any set previously). Any field listed in
        HEALTH_PRIVATE_FIELDS is always null in "row", regardless of what
        was just written for it.
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
            "heart_rate": heart_rate,
            "hrv_ms": hrv_ms,
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
            upsert_daily_metric_measurements(conn, day.isoformat(), provided)
            rows = daily_metrics_wide(conn, METRIC_COLUMNS, day.isoformat(), day.isoformat())
            row = rows[0] if rows else None
        finally:
            conn.close()
    except db_error_types() as exc:
        logger.error("Database error writing to %s: %s", HEALTH_DB_PATH, exc)
        code = ERR_DATABASE_LOCKED if _is_locked_error(exc) else ERR_DATABASE_ERROR
        raise _tool_error(
            code,
            "Could not write to the health database — it may be locked by another process. Try again in a moment.",
        ) from exc

    return LogDailyMetricResult(logged=provided, row=DailyMetricsRow(**_redact_private_fields(row)))


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
    weight entered with the wrong units. Clears *everything* recorded for
    that metric/day — including individual log_measurement readings or
    imported rows, not just a value log_daily_metric wrote directly — so
    the metric genuinely goes back to "nothing recorded" rather than
    falling back to a blended value from whatever else is left.

    Privacy note: this server and its SQLite file are entirely local, but
    the data returned by this tool becomes part of the conversation sent
    to whatever model the calling client is configured with. If that
    model runs in the cloud rather than on your machine, treat this the
    same as pasting the data into a chat with that provider.

    Args:
        date: The day to clear a field for, formatted YYYY-MM-DD.
        field: Which metric to blank out. One of: steps, sleep_hours,
            resting_heart_rate, weight_kg, workout_minutes, mood, water_ml, heart_rate, hrv_ms.

    Returns:
        A ClearMetricResult with "cleared" (the field name) and "row" (the
        day's full current state after clearing). If field was the only
        metric that date had any data for, "row" is null and "note" says
        so explicitly (clearing succeeded — there's just nothing left to
        show). If there was nothing to clear in the first place, "row" is
        also null but "note" says so instead. Any field listed in
        HEALTH_PRIVATE_FIELDS is always null in "row".
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
            deleted = clear_daily_metric(conn, day.isoformat(), field)
            rows = daily_metrics_wide(conn, METRIC_COLUMNS, day.isoformat(), day.isoformat())
            row = rows[0] if rows else None
        finally:
            conn.close()
    except db_error_types() as exc:
        logger.error("Database error writing to %s: %s", HEALTH_DB_PATH, exc)
        code = ERR_DATABASE_LOCKED if _is_locked_error(exc) else ERR_DATABASE_ERROR
        raise _tool_error(
            code,
            "Could not write to the health database — it may be locked by another process. Try again in a moment.",
        ) from exc

    if row is None:
        if deleted:
            # field was the only metric this date had any data for, so
            # clearing it left the date with nothing at all -- daily_metrics
            # (now one row per (date, metric), not one row per date) has
            # no row left to pivot into a DailyMetricsRow. Distinct from
            # the never-had-anything case below.
            return ClearMetricResult(cleared=field, note=f"Cleared — {day.isoformat()} now has no metrics recorded.")
        return ClearMetricResult(cleared=field, note=f"No row exists for {day.isoformat()} — nothing to clear.")
    return ClearMetricResult(cleared=field, row=DailyMetricsRow(**_redact_private_fields(row)))


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


@mcp.tool(
    annotations=ToolAnnotations(
        title="Log a raw measurement",
        readOnlyHint=False,
        destructiveHint=False,  # always inserts a new row, never overwrites one
        idempotentHint=False,  # calling it twice logs two measurements, not one
        openWorldHint=False,
    )
)
def log_measurement(
    timestamp: str,
    metric: str,
    value: float,
    unit: str | None = None,
    source: str | None = None,
    source_type: str | None = None,
) -> LogMeasurementResult:
    """
    Record a single raw observation — one metric, one value, one point in
    time — rather than a whole day's summary. Use this instead of
    log_daily_metric when the source, exact time, or the fact that there
    were *multiple* readings that day matters (e.g. three separate
    workouts, or a wearable's periodic heart-rate samples).

    Use this tool when:
    - recording one timestamped observation where the exact time, source,
      or possibility of multiple same-day readings matters (e.g. "record
      my blood pressure reading from my cuff at 7am").

    Do not use this tool when:
    - it's just a single end-of-day value for a fixed metric -> use
      `log_daily_metric` instead.
    - it's a workout/exercise session -> use `log_workout_session` instead.

    Privacy note: this server and its SQLite file are entirely local, but
    the data returned by this tool becomes part of the conversation sent
    to whatever model the calling client is configured with. If that
    model runs in the cloud rather than on your machine, treat this the
    same as pasting the data into a chat with that provider.

    Args:
        timestamp: When the observation was taken, YYYY-MM-DD or a full
            ISO 8601 timestamp (YYYY-MM-DDTHH:MM:SS).
        metric: Name of the metric, e.g. "resting_heart_rate", "steps".
            Not free-form: must already have an entry in the
            aggregation_rules table (steps, sleep_hours,
            resting_heart_rate, weight_kg, workout_minutes, mood,
            water_ml, heart_rate, hrv_ms, out of the box) — daily_metrics
            is a database-maintained projection over measurements (see
            db/schema.sql), so every metric written to it needs a known
            aggregation method (sum/mean/last) or there would be nothing
            telling the projection how to roll same-day readings up.
            metrics_schema lists the current set.
        value: The numeric reading.
        unit: Unit the value is in, e.g. "bpm", "kg". Optional.
        source: Where this came from, e.g. "Apple Watch", "manual". Optional.
        source_type: Category of source, e.g. "wearable", "manual", "app". Optional.

    Returns:
        A LogMeasurementResult with the stored row, including its new id.
    """
    try:
        parse_date(timestamp[:10], "timestamp")
    except ValueError as exc:
        raise _tool_error(ERR_INVALID_TIMESTAMP, str(exc)) from exc
    if not metric.strip():
        raise _tool_error(ERR_INVALID_METRIC, "metric must be a non-empty string.")

    try:
        conn = connect_writable(HEALTH_DB_PATH)
        try:
            ensure_schema(conn)
            new_id = insert_measurement(conn, timestamp, metric, value, unit, source, source_type)
            conn.row_factory = row_class()
            row = conn.execute("SELECT * FROM measurements WHERE id = ?", (new_id,)).fetchone()
        finally:
            conn.close()
    except db_error_types() as exc:
        # The measurements->daily_metrics triggers (db/schema.sql) reject
        # an INSERT for a metric with no aggregation_rules entry via
        # RAISE(ABORT, 'no aggregation_rules entry for metric') — surfaces
        # here as an ordinary db_error_types() exception (sqlite3.
        # IntegrityError, or the sqlcipher3 equivalent when encrypted; see
        # logic.db_error_types), so it's distinguished by message rather
        # than exception type to work under either driver.
        if "no aggregation_rules entry for metric" in str(exc):
            try:
                with _readonly_connection(HEALTH_DB_PATH) as ro_conn:
                    known = [r[0] for r in ro_conn.execute("SELECT metric FROM aggregation_rules ORDER BY metric")]
            except db_error_types():
                known = []
            raise _tool_error(
                ERR_INVALID_METRIC,
                f"{metric!r} has no aggregation_rules entry, so it can't be logged as a measurement."
                + (f" Supported metrics: {', '.join(known)}." if known else ""),
            ) from exc
        logger.error("Database error writing to %s: %s", HEALTH_DB_PATH, exc)
        code = ERR_DATABASE_LOCKED if _is_locked_error(exc) else ERR_DATABASE_ERROR
        raise _tool_error(
            code,
            "Could not write to the health database — it may be locked by another process. Try again in a moment.",
        ) from exc

    return LogMeasurementResult(measurement=MeasurementRow(**dict(row)))


@mcp.tool(
    annotations=ToolAnnotations(
        title="Read raw measurements",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def read_measurements(
    metric: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    source: str | None = None,
    limit: int = 200,
) -> ReadMeasurementsResult:
    """
    Read individual measurement rows (not the daily_metrics aggregate),
    most recent first. Use this to see exactly when and where each
    reading came from, rather than just a day's summarized value.

    Use this tool when:
    - the user wants raw/individual observations (e.g. "what measurements
      have I recorded?"), including their timestamp or source.

    Do not use this tool when:
    - the user wants a broad, multi-metric overview -> use
      `read_health_data` instead.
    - the user wants one metric's day-by-day history -> use
      `get_metric_history` instead.
    - the user wants workout sessions -> use `read_workout_sessions` instead.

    Privacy note: this server and its SQLite file are entirely local, but
    the data returned by this tool becomes part of the conversation sent
    to whatever model the calling client is configured with. If that
    model runs in the cloud rather than on your machine, treat this the
    same as pasting the data into a chat with that provider.

    Args:
        metric: Only return this metric. Omit for all metrics.
        start_date: Only return rows on/after this date (YYYY-MM-DD). Omit for no lower bound.
        end_date: Only return rows on/before this date (YYYY-MM-DD). Omit for no upper bound.
        source: Only return rows from this source, e.g. "Apple Watch". Omit for all sources.
        limit: Maximum rows to return (default 200).

    Returns:
        A ReadMeasurementsResult with the matching rows and a count.
    """
    try:
        conn = connect_writable(HEALTH_DB_PATH)
        try:
            ensure_schema(conn)
            conn.row_factory = row_class()
            rows = query_measurements(conn, metric=metric, start=start_date, end=end_date, source=source, limit=limit)
        finally:
            conn.close()
    except db_error_types() as exc:
        logger.error("Database error reading from %s: %s", HEALTH_DB_PATH, exc)
        raise _tool_error(ERR_DATABASE_ERROR, "Could not read the health database. Try again in a moment.") from exc

    return ReadMeasurementsResult(measurements=[MeasurementRow(**row) for row in rows], count=len(rows))


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


@mcp.tool(
    annotations=ToolAnnotations(
        title="Preview a source-priority resolution of a day's measurements",
        readOnlyHint=True,  # never writes daily_metrics -- see docstring
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def aggregate_measurements(date: str, source_priority: list[str] | None = None) -> AggregateMeasurementsResult:
    """
    Preview what one day's raw measurements would roll up to if a
    conflicting metric were resolved using source_priority, alongside
    that day's *actual* current daily_metrics values.

    daily_metrics is a database-maintained projection (see db/schema.sql):
    every log_measurement/import automatically keeps it in sync with
    *all* of that day's measurements the moment it's written, using each
    metric's fixed aggregation method (sum/mean/last — see
    aggregation_rules, or get_baseline's "method" field). It blends every
    source together and cannot be made to prefer one — there is no
    stored "priority" it can consult. So unlike before, this tool no
    longer writes anything: "aggregated" is only a preview of what
    source_priority would produce; "row" is the real, currently-stored
    value, computed from every source, which may well differ from
    "aggregated" whenever sources disagree.

    If a metric has measurements from more than one source that day (e.g.
    an Apple Watch and a Garmin both logging resting_heart_rate), use
    get_metric_provenance first to see whether they actually disagree. To
    make daily_metrics itself reflect only one source going forward,
    remove the other source's data — clear_metric (which now removes
    every measurement behind that metric/day, not just a
    log_daily_metric value) followed by re-logging the preferred
    reading, or re-running its import with --replace.

    Privacy note: this server and its SQLite file are entirely local, but
    the data returned by this tool becomes part of the conversation sent
    to whatever model the calling client is configured with. If that
    model runs in the cloud rather than on your machine, treat this the
    same as pasting the data into a chat with that provider.

    Args:
        date: The day to preview, formatted YYYY-MM-DD.
        source_priority: Ordered list of source names, e.g. ["Apple
            Watch", "Garmin"]. For any metric with more than one source
            that day, the first name in this list that's actually present
            wins in "aggregated" and the other source's readings for that
            metric are dropped from that preview. Omit to fall back to
            whichever source was imported most recently. Never affects
            "row" — see above.

    Returns:
        An AggregateMeasurementsResult with "aggregated" (the
        source_priority preview; only metrics with measurements that day
        are included) and "row" (that day's actual, currently-stored
        daily_metrics values — unaffected by source_priority).
    """
    try:
        day = parse_date(date, "date")
    except ValueError as exc:
        raise _tool_error(ERR_INVALID_DATE, str(exc)) from exc

    try:
        with _readonly_connection(HEALTH_DB_PATH) as conn:
            conn.row_factory = row_class()
            aggregated = aggregate_measurements_to_daily(conn, day.isoformat(), source_priority)
            metrics_only = {k: v for k, v in aggregated.items() if k != "date"}
            rows = daily_metrics_wide(conn, METRIC_COLUMNS, day.isoformat(), day.isoformat())
            row = rows[0] if rows else None
    except db_error_types() as exc:
        logger.error("Database error reading %s: %s", HEALTH_DB_PATH, exc)
        raise _tool_error(
            ERR_DATABASE_ERROR,
            "Could not read the health database — it may be missing or corrupt. Try again, or re-run init_db.py.",
        ) from exc

    row_dict = dict(row) if row is not None else {"date": day.isoformat()}
    return AggregateMeasurementsResult(
        date=day.isoformat(),
        aggregated=metrics_only,
        row=DailyMetricsRow(**_redact_private_fields(row_dict)),
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Break a metric down by source",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def get_metric_provenance(metric: str, date: str) -> GetMetricProvenanceResult:
    """
    Show one metric's raw measurements for one day, broken down by which
    source reported them — answers "which one is correct?" when e.g. an
    Apple Watch and a Garmin disagree on resting heart rate, instead of
    silently averaging two different devices into one number.

    Do not use this tool when:
    - the user just wants a plain day-by-day history for the metric, with
      no need to see the per-source breakdown -> use `get_metric_history`
      instead.

    Privacy note: this server and its SQLite file are entirely local, but
    the data returned by this tool becomes part of the conversation sent
    to whatever model the calling client is configured with. If that
    model runs in the cloud rather than on your machine, treat this the
    same as pasting the data into a chat with that provider.

    Args:
        metric: Name of the metric to inspect, e.g. "resting_heart_rate".
        date: The day to inspect, formatted YYYY-MM-DD.

    Returns:
        A GetMetricProvenanceResult listing each source's average value,
        reading count, and latest timestamp that day, plus "conflict"
        (true when 2+ sources disagree by more than a small tolerance).
    """
    try:
        day = parse_date(date, "date")
    except ValueError as exc:
        raise _tool_error(ERR_INVALID_DATE, str(exc)) from exc

    try:
        conn = connect_writable(HEALTH_DB_PATH)
        try:
            ensure_schema(conn)
            result = _get_metric_provenance(conn, metric, day.isoformat())
        finally:
            conn.close()
    except db_error_types() as exc:
        logger.error("Database error reading from %s: %s", HEALTH_DB_PATH, exc)
        raise _tool_error(ERR_DATABASE_ERROR, "Could not read the health database. Try again in a moment.") from exc

    return GetMetricProvenanceResult(**result)


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
        "evidence" — how much of the window this baseline is actually
        based on. When evidence.confidence is "moderate" or "low", say so
        when reporting the baseline (e.g. "based on only 60% of days")
        rather than stating mean/median as if they were computed from a
        complete series. All baseline fields are null and n is 0 if the
        metric has no data in range — not an error, since "nothing logged
        yet" is an expected state.
    """
    try:
        start, end = resolve_range(start_date, end_date, default_days=90)
    except ValueError as exc:
        raise _tool_error(ERR_INVALID_RANGE, str(exc)) from exc
    series = _fetch_metric_series(metric, start, end)
    return GetBaselineResult(
        metric=metric,
        range=DateRange(start_date=start.isoformat(), end_date=end.isoformat()),
        baseline=_baseline_stats(series),
        evidence=Evidence(**build_evidence(series, start, end)),
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Detect metric anomalies",
        readOnlyHint=True,
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
    return DetectAnomaliesResult(
        metric=metric,
        range=DateRange(start_date=start.isoformat(), end_date=end.isoformat()),
        threshold=threshold,
        anomalies=[AnomalyPoint(**a) for a in anomalies],
        evidence=Evidence(**build_evidence(series, start, end)),
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Calculate metric trend",
        readOnlyHint=True,
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
    return CalculateTrendResult(
        metric=metric,
        range=DateRange(start_date=start.isoformat(), end_date=end.isoformat()),
        trend=_trend_stats(series),
        evidence=Evidence(**build_evidence(series, start, end)),
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Compare two periods",
        readOnlyHint=True,
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
        "delta" (period_a mean minus period_b mean), "pct_change", and
        "period_a_evidence"/"period_b_evidence" for each period
        separately — the two periods can have very different coverage
        (e.g. this month is 90% logged, last month only 40%), and that
        asymmetry matters more than either evidence object alone. If
        either period's confidence is "moderate" or "low", say the
        comparison rests on incomplete data for that period rather than
        stating the delta/pct_change as a clean before/after. Both delta
        and pct_change are null if either period has no data at all.
    """
    try:
        start_a, end_a = resolve_range(period_a_start, period_a_end, default_days=0)
        start_b, end_b = resolve_range(period_b_start, period_b_end, default_days=0)
    except ValueError as exc:
        raise _tool_error(ERR_INVALID_RANGE, str(exc)) from exc
    series_a = _fetch_metric_series(metric, start_a, end_a)
    series_b = _fetch_metric_series(metric, start_b, end_b)
    result = compare_periods(series_a, series_b)
    return ComparePeriodsResult(
        metric=metric,
        period_a=DateRange(start_date=start_a.isoformat(), end_date=end_a.isoformat()),
        period_b=DateRange(start_date=start_b.isoformat(), end_date=end_b.isoformat()),
        period_a_stats=BaselineStats(**result["period_a"]),
        period_b_stats=BaselineStats(**result["period_b"]),
        delta=result["delta"],
        pct_change=result["pct_change"],
        period_a_evidence=Evidence(**build_evidence(series_a, start_a, end_a)),
        period_b_evidence=Evidence(**build_evidence(series_b, start_b, end_b)),
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Find correlation between two metrics",
        readOnlyHint=True,
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
        A CorrelationResult with "r" (-1 to 1), "n" (overlapping days
        used), "sample_confidence" (a bucketed read on "n" alone — see
        analytics._sample_confidence — that makes no claim about "r"
        itself), and "evidence_a"/"evidence_b" for each metric's own
        coverage over the window (independent of "n" — a metric can have
        low overall coverage yet still have enough overlapping days to
        produce an "r"). "r" is null with fewer than 4 overlapping days.
        If either evidence's confidence is "moderate" or "low", or if
        sample_confidence is anything short of "strong_sample", say the
        correlation is based on a thin sample or gappy series rather than
        reporting "r" as a settled relationship.
    """
    try:
        start, end = resolve_range(start_date, end_date, default_days=90)
    except ValueError as exc:
        raise _tool_error(ERR_INVALID_RANGE, str(exc)) from exc
    series_a = _fetch_metric_series(metric_a, start, end)
    series_b = _fetch_metric_series(metric_b, start, end)
    result = find_correlations(series_a, series_b, lag_days=lag_days)
    return CorrelationResult(
        metric_a=metric_a,
        metric_b=metric_b,
        **result,
        evidence_a=Evidence(**build_evidence(series_a, start, end)),
        evidence_b=Evidence(**build_evidence(series_b, start, end)),
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
                )
            )

        for anomaly in detect_anomalies(recent_series):
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
                )
            )

        trend = calculate_trend(recent_series)
        if trend["direction"] not in ("flat", "insufficient_data") and (trend["r_squared"] or 0) >= 0.3:
            changes.append(
                ChangeNote(
                    metric=metric,
                    kind="trend",
                    detail=f"{metric} has been {trend['direction']} over the last {days} days "
                    f"({trend['slope_per_day']:+g}/day, r²={trend['r_squared']}).",
                    evidence=metric_evidence,
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
        "baseline_evidence" and "trend_evidence" report coverage for the
        90-day and 30-day windows respectively; each correlated metric
        carries its own pair too. "narrative_facts" restates the above as
        short plain-English sentences but does NOT itself hedge on
        coverage — if baseline_evidence or trend_evidence has "moderate"
        or "low" confidence, add that caveat yourself when turning these
        facts into an answer, rather than presenting them as equally
        solid regardless of how much data backs each one.
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
    for other in METRIC_COLUMNS:
        if other == metric or other in PRIVATE_FIELDS:
            continue
        other_series = _fetch_metric_series(other, baseline_start, target_day)
        result = find_correlations(series, other_series, lag_days=0)
        if result["r"] is not None and abs(result["r"]) >= 0.5:
            correlated.append(
                CorrelationResult(
                    metric_a=metric,
                    metric_b=other,
                    **result,
                    evidence_a=Evidence(**build_evidence(series, baseline_start, target_day)),
                    evidence_b=Evidence(**build_evidence(other_series, baseline_start, target_day)),
                )
            )
    correlated.sort(key=lambda c: abs(c.r or 0), reverse=True)
    correlated = correlated[:5]

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
        baseline_evidence=Evidence(**build_evidence(series, baseline_start, target_day)),
        trend_evidence=Evidence(**build_evidence(trend_series, trend_start, target_day)),
        conflicting_days=conflicting_days,
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


class MetricDefinition(BaseModel):
    name: str
    min: float
    max: float
    label: str
    private: bool  # mirrors PRIVATE_FIELDS at the time this is read


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
    mcp.run()


if __name__ == "__main__":
    main()
