"""
Workout tools
==============

log_workout_session and read_workout_sessions, moved out of server.py
verbatim. Each tool body is unchanged; only the one server.py
global/helper each referenced is now a closure-bound parameter of
register_workout_tools instead of a module-level name: HEALTH_DB_PATH
-> db_path. logger is passed through under its original name.
read_workout_sessions uses connect_writable, not a readonly_connection
helper, despite its readOnlyHint=True annotation — pre-existing
behavior, unchanged by this move. See server.py for the call that
wires these back in.
"""

from mcp.types import ToolAnnotations

from errors import (
    ERR_DATABASE_ERROR,
    ERR_DATABASE_LOCKED,
    ERR_INVALID_ACTIVITY_TYPE,
    ERR_INVALID_DATE,
    ERR_INVALID_DURATION,
    ERR_INVALID_INTENSITY,
    _is_locked_error,
    _tool_error,
)
from logic import (
    WORKOUT_INTENSITIES,
    connect_writable,
    db_error_types,
    ensure_schema,
    insert_workout_session,
    parse_date,
    query_workout_sessions,
    row_class,
)
from schemas import (
    LogWorkoutSessionResult,
    ReadWorkoutSessionsResult,
    WorkoutSessionRow,
)


def register_workout_tools(
    mcp,
    *,
    db_path,
    logger,
):
    """Register the two workout tools on mcp, and also return the raw
    (undecorated) functions themselves.

    @mcp.tool(...) is a side-effecting registration decorator, not a
    wrapping one -- FastMCP's LocalProvider.tool() calls self.add_tool(fn)
    then returns fn unchanged (see fastmcp/server/providers/local_provider/
    local_provider.py). When these two were top-level functions in
    server.py, that meant server.log_workout_session (etc.) was always the
    plain, directly callable function, registration aside. Nested here as
    closures instead, they're no longer bound to any module-level name on
    their own, so this return value is how server.py restores
    server.log_workout_session and server.read_workout_sessions to exactly
    what they were.
    """

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
            conn = connect_writable(db_path)
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
            logger.error("Database error writing to %s: %s", db_path, exc)
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
            conn = connect_writable(db_path)
            try:
                ensure_schema(conn)
                conn.row_factory = row_class()
                rows = query_workout_sessions(
                    conn, start=start_date, end=end_date, activity_type=activity_type, limit=limit
                )
            finally:
                conn.close()
        except db_error_types() as exc:
            logger.error("Database error reading from %s: %s", db_path, exc)
            raise _tool_error(ERR_DATABASE_ERROR, "Could not read the health database. Try again in a moment.") from exc

        return ReadWorkoutSessionsResult(sessions=[WorkoutSessionRow(**row) for row in rows], count=len(rows))

    return log_workout_session, read_workout_sessions
