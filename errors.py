"""
Error semantics
================

The stable, machine-parseable error codes every tool in server.py raises
against, plus the two small helpers that build/classify them. Moved out
of server.py verbatim — no behavior, wording, or payload structure
changed. See server.py for where these are used.
"""

from fastmcp.exceptions import ToolError

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
