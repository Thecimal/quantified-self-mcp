"""
Data status tools
=================

get_data_status and get_import_status: read-only answers to "how current is
my data?" and "what did the last import do?", so an analysis is never run
on a stale or half-imported database without anyone noticing. The
classification itself lives in logic.compute_data_status; this module only
wraps it for MCP. private_fields is passed in by server.py (rather than
imported here) for the same reason tools/health.py takes its connection
helper as a parameter: it must reflect the server module's own import.
"""

from datetime import date

from mcp.types import ToolAnnotations
from pydantic import BaseModel

from errors import (
    ERR_DATABASE_ERROR,
    ERR_DATABASE_LOCKED,
    ERR_INVALID_RANGE,
    _is_locked_error,
    _tool_error,
)
from logic import (
    DEFAULT_STALE_AFTER_DAYS,
    compute_data_status,
    db_error_types,
    list_imports,
)

MAX_STALE_AFTER_DAYS = 3660
MAX_IMPORTS_RETURNED = 50


class ImportRecord(BaseModel):
    id: int
    importer: str
    source_file: str | None = None
    source_sha256: str | None = None
    status: str
    started_at: str
    finished_at: str | None = None
    rows_loaded: int | None = None
    rows_skipped: int | None = None
    measurements_written: int | None = None
    records_seen: int | None = None
    records_added: int | None = None
    records_updated: int | None = None
    records_unchanged: int | None = None
    records_removed: int | None = None
    coverage_before_start: str | None = None
    coverage_before_end: str | None = None
    coverage_after_start: str | None = None
    coverage_after_end: str | None = None
    error: str | None = None


class DataCoverage(BaseModel):
    start: str | None = None
    end: str | None = None


class DataGap(BaseModel):
    start: str
    end: str
    days: int


class DataStatusResult(BaseModel):
    status: str
    reason: str | None = None
    action: str | None = None
    as_of: str
    latest_data: str | None = None
    days_behind: int | None = None
    stale_after_days: int
    coverage: DataCoverage
    days_with_data: int
    gap_count: int
    missing_days: int
    gaps: list[DataGap]
    source: str | None = None
    last_successful_import: str | None = None
    latest_import: ImportRecord | None = None


class GetImportStatusResult(BaseModel):
    imports: list[ImportRecord]


def register_status_tools(
    mcp,
    *,
    db_path,
    logger,
    readonly_connection,
    private_fields,
):
    """Register get_data_status and get_import_status on mcp, and return the
    raw functions (see register_health_tools for why they are returned)."""

    def _database_error(exc: Exception):
        logger.error("Database error reading %s: %s", db_path, exc)
        if _is_locked_error(exc):
            return _tool_error(
                ERR_DATABASE_LOCKED,
                "Could not read the health database — it is locked by another process. Try again in a moment.",
            )
        return _tool_error(
            ERR_DATABASE_ERROR,
            "Could not read the health database — it may be missing or corrupt. Try again, or re-run init_db.py.",
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Check how current the data is",
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )
    )
    def get_data_status(stale_after_days: int = DEFAULT_STALE_AFTER_DAYS) -> DataStatusResult:
        """
        Report how current and complete the database is: latest data date,
        coverage window, gaps, last successful import, and an overall status
        of CURRENT, STALE, INCOMPLETE, NO_DATA or IMPORT_FAILED. Check this
        before drawing conclusions from any analysis, and say so when the
        status is anything other than CURRENT.

        Use this tool when:
        - the user asks how up to date their data is, or whether an import
          is needed.
        - you are about to interpret recent data and want to know whether it
          is fresh enough to trust.

        Privacy note: this server and its SQLite file are entirely local, but
        the data returned by this tool becomes part of the conversation sent
        to whatever model the calling client is configured with. If that
        model runs in the cloud rather than on your machine, treat this the
        same as pasting the data into a chat with that provider.

        Args:
            stale_after_days: How many days the latest data may trail today
                before the status becomes STALE. Defaults to 2.

        Returns:
            A DataStatusResult with "status", a machine-readable "reason" and
            suggested "action" when the status is not CURRENT, "latest_data",
            "days_behind", "coverage" (start/end), "gap_count",
            "missing_days" and the most recent "gaps", the importer ("source")
            and time of the last successful import, and the "latest_import"
            record. Metrics listed in HEALTH_PRIVATE_FIELDS are ignored.
        """
        if not 0 <= stale_after_days <= MAX_STALE_AFTER_DAYS:
            raise _tool_error(ERR_INVALID_RANGE, f"stale_after_days must be between 0 and {MAX_STALE_AFTER_DAYS}.")
        try:
            with readonly_connection(db_path) as conn:
                status = compute_data_status(
                    conn, date.today(), stale_after_days=stale_after_days, exclude_metrics=private_fields
                )
        except db_error_types() as exc:
            raise _database_error(exc) from exc
        return DataStatusResult(**status)

    @mcp.tool(
        annotations=ToolAnnotations(
            title="List recent imports",
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )
    )
    def get_import_status(limit: int = 5) -> GetImportStatusResult:
        """
        List the most recent import runs, newest first: which importer, the
        source file's name and SHA-256, whether the run succeeded, failed or
        was interrupted, and how many rows were loaded, skipped and written.

        Use this tool when:
        - the user asks what was imported and when, or why an import did not
          go through.

        Privacy note: this server and its SQLite file are entirely local, but
        the data returned by this tool becomes part of the conversation sent
        to whatever model the calling client is configured with. If that
        model runs in the cloud rather than on your machine, treat this the
        same as pasting the data into a chat with that provider.

        Args:
            limit: How many import runs to return (1-50). Defaults to 5.

        Returns:
            A GetImportStatusResult whose "imports" lists each run. A run
            still marked "running" long after it started was interrupted.
        """
        if not 1 <= limit <= MAX_IMPORTS_RETURNED:
            raise _tool_error(ERR_INVALID_RANGE, f"limit must be between 1 and {MAX_IMPORTS_RETURNED}.")
        try:
            with readonly_connection(db_path) as conn:
                imports = list_imports(conn, limit=limit)
        except db_error_types() as exc:
            raise _database_error(exc) from exc
        return GetImportStatusResult(imports=[ImportRecord(**record) for record in imports])

    return get_data_status, get_import_status
