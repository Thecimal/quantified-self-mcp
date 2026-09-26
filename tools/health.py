"""
Health tools
=============

read_health_data, export_health_data_csv, log_daily_metric, and
clear_metric, moved out of server.py verbatim. Each tool body is
unchanged; only three server.py globals/helpers it referenced are now
closure-bound parameters of register_health_tools instead of
module-level names: HEALTH_DB_PATH -> db_path, _readonly_connection ->
readonly_connection, METRIC_COLUMNS -> metric_columns. logger is passed
through under its original name. See server.py for the call that wires
these back in.
"""

import csv

from mcp.types import ToolAnnotations

from errors import (
    ERR_DATABASE_ERROR,
    ERR_DATABASE_LOCKED,
    ERR_INVALID_DATE,
    ERR_INVALID_FIELD,
    ERR_INVALID_METRIC_VALUE,
    ERR_INVALID_RANGE,
    ERR_MISSING_METRIC,
    _is_locked_error,
    _tool_error,
)
from evidence import build_coverage_summary
from logic import (
    MAX_ROWS_RETURNED,
    clear_daily_metric,
    connect_writable,
    daily_metrics_wide,
    db_error_types,
    ensure_schema,
    numeric_stats,
    parse_date,
    resolve_range,
    upsert_daily_metric_measurements,
    validate_metrics,
)
from privacy import PRIVATE_FIELDS, _redact_private_fields
from schemas import (
    ClearMetricResult,
    CoverageSummary,
    DailyMetricsRow,
    DateRange,
    ExportCsvResult,
    HealthDataSummary,
    LogDailyMetricResult,
    MetricStats,
    ReadHealthDataResult,
)


def register_health_tools(
    mcp,
    *,
    db_path,
    logger,
    readonly_connection,
    metric_columns,
):
    """Register the four health tools on mcp, and also return the raw
    (undecorated) functions themselves.

    @mcp.tool(...) is a side-effecting registration decorator, not a
    wrapping one -- FastMCP's LocalProvider.tool() calls self.add_tool(fn)
    then returns fn unchanged (see fastmcp/server/providers/local_provider/
    local_provider.py). When these four were top-level functions in
    server.py, that meant server.read_health_data (etc.) was always the
    plain, directly callable function, registration aside -- several tests
    in tests/test_server.py call health_db.log_daily_metric(...) and
    health_db.export_health_data_csv(...) exactly that way, not through the
    MCP Client protocol. Nested here as closures instead, they're no longer
    bound to any module-level name on their own, so this return value is
    how server.py restores server.read_health_data and friends to exactly
    what they were.
    """
    @mcp.tool(
        annotations=ToolAnnotations(
            title="Read health data",
            readOnlyHint=True,  # opened via readonly_connection; cannot write
            destructiveHint=False,
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
            with readonly_connection(db_path) as conn:
                rows = daily_metrics_wide(conn, metric_columns, start.isoformat(), end.isoformat())
        except db_error_types() as exc:
            logger.error("Database error reading %s: %s", db_path, exc)
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

        public_metrics = [m for m in metric_columns if m not in PRIVATE_FIELDS]
        return ReadHealthDataResult(
            range=DateRange(start_date=start.isoformat(), end_date=end.isoformat()),
            rows=[DailyMetricsRow(**_redact_private_fields(row)) for row in returned_rows],
            truncated=truncated,
            summary=HealthDataSummary(
                days_with_data=len(rows),
                **{
                    metric: (MetricStats() if metric in PRIVATE_FIELDS else MetricStats(**numeric_stats(rows, metric)))
                    for metric in metric_columns
                },
            ),
            coverage=CoverageSummary(**build_coverage_summary(rows, start, end, public_metrics)),
        )


    @mcp.tool(
        annotations=ToolAnnotations(
            title="Export health data to CSV",
            readOnlyHint=False,  # writes a CSV file to the local exports/ directory
            destructiveHint=True,  # opens the file with mode "w": an existing export for the same range is overwritten
            idempotentHint=True,  # deterministic filename per date range -> repeat calls rewrite the same content
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
            with readonly_connection(db_path) as conn:
                rows = daily_metrics_wide(conn, metric_columns, start.isoformat(), end.isoformat())
        except db_error_types() as exc:
            logger.error("Database error reading %s: %s", db_path, exc)
            if _is_locked_error(exc):
                raise _tool_error(
                    ERR_DATABASE_LOCKED,
                    "Could not read the health database — it is locked by another process. Try again in a moment.",
                ) from exc
            raise _tool_error(
                ERR_DATABASE_ERROR,
                "Could not read the health database — it may be missing or corrupt. Try again, or re-run init_db.py.",
            ) from exc

        export_dir = db_path.parent / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        out_path = export_dir / f"health_export_{start.isoformat()}_to_{end.isoformat()}.csv"

        with out_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["date", *metric_columns])
            for row in rows:
                # Routed through DailyMetricsRow (not just _redact_private_fields)
                # so int-typed metrics come out as ints, not the float SQLite's
                # REAL-typed daily_metrics.value column hands back — the same
                # coercion read_health_data/log_daily_metric already get for
                # free by constructing a DailyMetricsRow.
                typed = DailyMetricsRow(**_redact_private_fields(row)).model_dump()
                writer.writerow([typed["date"], *(typed[col] for col in metric_columns)])

        return ExportCsvResult(
            path=str(out_path.resolve()),
            rows_exported=len(rows),
            range=DateRange(start_date=start.isoformat(), end_date=end.isoformat()),
        )


    @mcp.tool(
        annotations=ToolAnnotations(
            title="Log a daily metric",
            readOnlyHint=False,
            # upsert_daily_metric_measurements DELETEs the previous daily-log
            # measurement for each (metric, day) and inserts the new one, so a
            # previously logged value is replaced, not just added to.
            destructiveHint=True,
            idempotentHint=True,  # re-sending the same values leaves the same state
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
            conn = connect_writable(db_path)
            try:
                ensure_schema(conn)
                upsert_daily_metric_measurements(conn, day.isoformat(), provided)
                rows = daily_metrics_wide(conn, metric_columns, day.isoformat(), day.isoformat())
                row = rows[0] if rows else None
            finally:
                conn.close()
        except db_error_types() as exc:
            logger.error("Database error writing to %s: %s", db_path, exc)
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

        if field not in metric_columns:
            raise _tool_error(ERR_INVALID_FIELD, f"field must be one of: {', '.join(metric_columns)} — got {field!r}")

        try:
            conn = connect_writable(db_path)
            try:
                ensure_schema(conn)
                deleted = clear_daily_metric(conn, day.isoformat(), field)
                rows = daily_metrics_wide(conn, metric_columns, day.isoformat(), day.isoformat())
                row = rows[0] if rows else None
            finally:
                conn.close()
        except db_error_types() as exc:
            logger.error("Database error writing to %s: %s", db_path, exc)
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
                return ClearMetricResult(
                    cleared=field, note=f"Cleared — {day.isoformat()} now has no metrics recorded."
                )
            return ClearMetricResult(cleared=field, note=f"No row exists for {day.isoformat()} — nothing to clear.")
        return ClearMetricResult(cleared=field, row=DailyMetricsRow(**_redact_private_fields(row)))

    return read_health_data, export_health_data_csv, log_daily_metric, clear_metric
