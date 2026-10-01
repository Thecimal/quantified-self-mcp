"""
Measurements tools
===================

log_measurement, read_measurements, aggregate_measurements, and
get_metric_provenance, moved out of server.py verbatim. Each tool body
is unchanged; only three server.py globals/helpers it referenced are
now closure-bound parameters of register_measurement_tools instead of
module-level names: HEALTH_DB_PATH -> db_path, _readonly_connection ->
readonly_connection, METRIC_COLUMNS -> metric_columns. logger is passed
through under its original name. See server.py for the call that wires
these back in.
"""

from mcp.types import ToolAnnotations

from errors import (
    ERR_DATABASE_ERROR,
    ERR_DATABASE_LOCKED,
    ERR_INVALID_DATE,
    ERR_INVALID_FIELD,
    ERR_INVALID_METRIC,
    ERR_INVALID_METRIC_VALUE,
    ERR_INVALID_TIMESTAMP,
    _is_locked_error,
    _tool_error,
)
from logic import (
    InvalidTimestampError,
    aggregate_measurements_to_daily,
    connect_writable,
    daily_metrics_wide,
    db_error_types,
    ensure_schema,
    insert_measurement,
    parse_date,
    query_measurements,
    row_class,
    validate_metrics,
)
from logic import (
    get_metric_provenance as _get_metric_provenance,
)
from privacy import PRIVATE_FIELDS, _redact_private_fields
from schemas import (
    AggregateMeasurementsResult,
    DailyMetricsRow,
    GetMetricProvenanceResult,
    LogMeasurementResult,
    MeasurementRow,
    ReadMeasurementsResult,
)


def _redact_measurement(row: dict) -> dict:
    """Null the value of a raw measurement row whose metric is in HEALTH_PRIVATE_FIELDS (the value stays
    stored locally; it just never reaches the model), mirroring what read_health_data does for daily rows."""
    return {**row, "value": None} if row["metric"] in PRIVATE_FIELDS else row


def _refuse_private_metric(metric: str) -> None:
    """Metric-specific tools refuse a private metric outright, like get_metric_history and the analytics
    tools: a flag derived from private values (e.g. provenance's `conflict`) would leak their shape even
    with the numbers nulled."""
    if metric in PRIVATE_FIELDS:
        raise _tool_error(
            ERR_INVALID_FIELD,
            f"{metric!r} is configured as private (HEALTH_PRIVATE_FIELDS); its measurements can't be returned.",
        )


def register_measurement_tools(
    mcp,
    *,
    db_path,
    logger,
    readonly_connection,
    metric_columns,
):
    """Register the four measurements tools on mcp, and also return the raw
    (undecorated) functions themselves.

    @mcp.tool(...) is a side-effecting registration decorator, not a
    wrapping one -- FastMCP's LocalProvider.tool() calls self.add_tool(fn)
    then returns fn unchanged (see fastmcp/server/providers/local_provider/
    local_provider.py). When these four were top-level functions in
    server.py, that meant server.log_measurement (etc.) was always the
    plain, directly callable function, registration aside -- several tests
    in tests/test_server.py call health_db.log_measurement(...) and
    health_db.aggregate_measurements(...) exactly that way, not through the
    MCP Client protocol. Nested here as closures instead, they're no longer
    bound to any module-level name on their own, so this return value is
    how server.py restores server.log_measurement and friends to exactly
    what they were.
    """
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
        # Same bounds log_daily_metric and the CSV importers enforce. Without it an absurd value is stored
        # and flows into daily_metrics: mood=99 corrupts baselines and claims, and e.g. steps=1e308 makes
        # get_baseline/trend/explain/recent-changes raise OverflowError. Also rejects NaN/inf (the
        # comparison is False) before they reach SQLite.
        try:
            validate_metrics({metric: value})
        except ValueError as exc:
            raise _tool_error(ERR_INVALID_METRIC_VALUE, str(exc)) from exc

        try:
            conn = connect_writable(db_path)
            try:
                ensure_schema(conn)
                new_id = insert_measurement(conn, timestamp, metric, value, unit, source, source_type)
                conn.row_factory = row_class()
                row = conn.execute("SELECT * FROM measurements WHERE id = ?", (new_id,)).fetchone()
            finally:
                conn.close()
        except InvalidTimestampError as exc:
            raise _tool_error(ERR_INVALID_TIMESTAMP, str(exc)) from exc
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
                    with readonly_connection(db_path) as ro_conn:
                        known = [r[0] for r in ro_conn.execute("SELECT metric FROM aggregation_rules ORDER BY metric")]
                except db_error_types():
                    known = []
                raise _tool_error(
                    ERR_INVALID_METRIC,
                    f"{metric!r} has no aggregation_rules entry, so it can't be logged as a measurement."
                    + (f" Supported metrics: {', '.join(known)}." if known else ""),
                ) from exc
            logger.error("Database error writing to %s: %s", db_path, exc)
            code = ERR_DATABASE_LOCKED if _is_locked_error(exc) else ERR_DATABASE_ERROR
            raise _tool_error(
                code,
                "Could not write to the health database — it may be locked by another process. Try again in a moment.",
            ) from exc

        return LogMeasurementResult(measurement=MeasurementRow(**_redact_measurement(dict(row))))

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
        if metric is not None:
            _refuse_private_metric(metric)
        try:
            conn = connect_writable(db_path)
            try:
                ensure_schema(conn)
                conn.row_factory = row_class()
                rows = query_measurements(
                    conn, metric=metric, start=start_date, end=end_date, source=source, limit=limit
                )
            finally:
                conn.close()
        except db_error_types() as exc:
            logger.error("Database error reading from %s: %s", db_path, exc)
            raise _tool_error(ERR_DATABASE_ERROR, "Could not read the health database. Try again in a moment.") from exc

        return ReadMeasurementsResult(
            measurements=[MeasurementRow(**_redact_measurement(dict(row))) for row in rows], count=len(rows)
        )

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
        every log_measurement/import automatically keeps it in sync the
        moment it's written, using each metric's fixed aggregation method
        (sum/mean/last — see aggregation_rules, or get_baseline's "method"
        field). When a metric has measurements from more than one source on
        a day, the projection uses only one source's observations — never a
        blend of devices. It takes the highest-ranked present source in the
        stored source priority list (manual log_daily_metric entries rank
        first by default); if none is ranked, the source that observed the
        most hours of that day, then the one with the latest observation,
        then by name. This tool writes nothing: "aggregated" previews what
        the source_priority you pass would produce; "row" is the real,
        currently-stored value, which differs from "aggregated" whenever
        the stored priority differs from the one you pass.

        If a metric has measurements from more than one source that day (e.g.
        an Apple Watch and a Garmin both logging resting_heart_rate), use
        get_metric_provenance first to see whether they actually disagree.

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
                metric are dropped from that preview. Omit to use the stored
                priority list and the same fallback the stored projection
                uses. Never affects "row" — see above.

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
            with readonly_connection(db_path) as conn:
                conn.row_factory = row_class()
                aggregated = aggregate_measurements_to_daily(conn, day.isoformat(), source_priority)
                metrics_only = {k: v for k, v in aggregated.items() if k != "date" and k not in PRIVATE_FIELDS}
                rows = daily_metrics_wide(conn, metric_columns, day.isoformat(), day.isoformat())
                row = rows[0] if rows else None
        except db_error_types() as exc:
            logger.error("Database error reading %s: %s", db_path, exc)
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
        _refuse_private_metric(metric)
        try:
            day = parse_date(date, "date")
        except ValueError as exc:
            raise _tool_error(ERR_INVALID_DATE, str(exc)) from exc

        try:
            conn = connect_writable(db_path)
            try:
                ensure_schema(conn)
                result = _get_metric_provenance(conn, metric, day.isoformat())
            finally:
                conn.close()
        except db_error_types() as exc:
            logger.error("Database error reading from %s: %s", db_path, exc)
            raise _tool_error(ERR_DATABASE_ERROR, "Could not read the health database. Try again in a moment.") from exc

        return GetMetricProvenanceResult(**result)

    return log_measurement, read_measurements, aggregate_measurements, get_metric_provenance
