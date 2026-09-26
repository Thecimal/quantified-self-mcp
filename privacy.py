"""
Field-level privacy
====================

Metrics listed in HEALTH_PRIVATE_FIELDS (comma-separated) are never
exposed by any tool, no matter how they're stored — read_health_data
always reports them as null (in both "rows" and "summary"), and the
"row" echoed back by log_daily_metric/clear_metric redacts them too, so
even the write tools' own responses can't leak a value back to the
model. The actual value is still written to and kept in the database
(so e.g. weight_kg can still be logged for your own records), just never
read back through the MCP tools. Unknown names are logged and ignored
rather than crashing the server, since a typo in this config shouldn't
take down the whole thing.

Moved out of server.py verbatim — no behavior, parsing, or redaction
semantics changed. See server.py for where these are used.
"""

import logging
import os

from metric_registry import INT_METRIC_KEYS, METRIC_KEYS

# Uses server.py's own logger name, not a new one, so a warning here
# shows up under the same logger identity it always has — this module is
# always imported by server.py (directly or transitively), which is what
# configures logging.basicConfig; a standalone import of privacy.py alone
# would fall back to logging's lastResort handler for this warning, same
# as any other unconfigured logger.
logger = logging.getLogger("quantified-self-mcp")


def _parse_private_fields(raw: str) -> frozenset[str]:
    names = {name.strip() for name in raw.split(",") if name.strip()}
    unknown = names - set(METRIC_KEYS)
    if unknown:
        logger.warning(
            "HEALTH_PRIVATE_FIELDS contains unknown field(s) %s; ignoring. Valid fields: %s",
            sorted(unknown),
            ", ".join(METRIC_KEYS),
        )
    return frozenset(names & set(METRIC_KEYS))


PRIVATE_FIELDS = _parse_private_fields(os.environ.get("HEALTH_PRIVATE_FIELDS", ""))


# daily_metrics.value is stored as REAL regardless of a metric's logical
# type (see db/schema.sql), so a "mean"-method metric can come back with a
# genuine fractional part -- e.g. two resting_heart_rate readings of 62
# and 67 average to 64.5 -- that DailyMetricsRow's `int` fields would
# otherwise reject outright rather than silently truncate. Derived from
# metric_registry (value_type == "int"); the pydantic models' annotations
# must agree with it, which tests/test_metric_registry.py checks.
INT_METRIC_COLUMNS = INT_METRIC_KEYS


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
