"""
log_measurement: a reading belongs to the calendar day written in its timestamp, not to the UTC day.

Every timestamp in `measurements` is local wall-clock time (see CHANGELOG, "Apple Health import day-shift bug"):
daily_metrics buckets days with SQLite date(timestamp), which converts an offset-aware string to UTC first, so an
evening reading with an offset such as -08:00 would be filed under the next day. log_measurement accepted such
timestamps and stored them as given, so it had the same bug. A numeric UTC offset is now dropped before storing;
"Z" (no local information) and naive timestamps are stored exactly as before.

All readings are `steps` from one source, so each day's value is the sum of what was written on it. Expected days
are worked out by hand in the comments; the UTC day each one used to land on is noted for contrast.
"""

import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from eval.agent.runner import loaded_server  # noqa: E402

# (timestamp, steps, day it belongs to, UTC day it used to be filed under)
READINGS = [
    # Evening in UTC-8, 30 minutes before local midnight: 2026-03-02 (UTC says 03-03).
    ("2026-03-02T23:30:00-08:00", 100, "2026-03-02", "2026-03-03"),
    # Just after local midnight in UTC+4: 2026-03-03 (UTC says 03-02, the day before).
    ("2026-03-03T00:30:00+04:00", 10, "2026-03-03", "2026-03-02"),
    # The same instant as the first reading, written in UTC. "Z" carries no local time, so it keeps its own day.
    ("2026-03-03T07:30:00Z", 1000, "2026-03-03", "2026-03-03"),
    # A naive timestamp is already wall-clock time.
    ("2026-03-02T23:45:00", 1, "2026-03-02", "2026-03-02"),
    # US spring-forward (2026-03-08): the evening before (PST) and the evening after (PDT) each keep their own day.
    ("2026-03-07T23:30:00-08:00", 20, "2026-03-07", "2026-03-08"),
    ("2026-03-08T23:30:00-07:00", 30, "2026-03-08", "2026-03-09"),
    # US fall-back (2026-11-01): 01:30 happens twice, an hour apart. Both are real readings on the same day.
    ("2026-11-01T01:30:00-07:00", 5, "2026-11-01", "2026-11-01"),
    ("2026-11-01T01:30:00-08:00", 7, "2026-11-01", "2026-11-01"),
]
# 03-02: 100 + 1 = 101   03-03: 10 + 1000 = 1010   03-07: 20   03-08: 30   11-01: 5 + 7 = 12
EXPECTED_DAILY = {"2026-03-02": 101, "2026-03-03": 1010, "2026-03-07": 20, "2026-03-08": 30, "2026-11-01": 12}


async def test_log_measurement_files_each_reading_under_the_day_it_was_written_on(tmp_path):
    with loaded_server(tmp_path / "health.db") as server:
        async with Client(server.mcp) as client:
            for timestamp, steps, _day, _utc_day in READINGS:
                await client.call_tool(
                    "log_measurement", {"timestamp": timestamp, "metric": "steps", "value": steps, "source": "Watch"}
                )
            daily = (
                await client.call_tool("read_health_data", {"start_date": "2026-03-01", "end_date": "2026-11-30"})
            ).structured_content["rows"]
            stored = (
                await client.call_tool("read_measurements", {"metric": "steps", "limit": 100})
            ).structured_content["measurements"]

            # An offset outside the range SQLite accepts is still rejected, and nothing is written for it.
            with pytest.raises(ToolError, match="invalid_timestamp"):
                await client.call_tool(
                    "log_measurement",
                    {"timestamp": "2026-03-02T10:00:00+25:00", "metric": "steps", "value": 999, "source": "Watch"},
                )
            after_reject = (
                await client.call_tool("read_measurements", {"metric": "steps", "limit": 100})
            ).structured_content["count"]

    # --- each reading is on the day written in its timestamp; the contrast cases show why this is not the UTC day ---
    assert {r["date"]: r["steps"] for r in daily} == EXPECTED_DAILY
    assert all(day != utc_day for _t, _s, day, utc_day in READINGS[:2] + READINGS[4:6])

    # --- no reading was lost or duplicated: the grand total is unchanged ---
    assert sum(EXPECTED_DAILY.values()) == sum(steps for _t, steps, _d, _u in READINGS) == 1173

    # --- a numeric offset is dropped from the stored timestamp; "Z" and naive timestamps are kept as given ---
    stored_by_value = {m["value"]: m["timestamp"] for m in stored}
    assert stored_by_value[100] == "2026-03-02T23:30:00"
    assert stored_by_value[10] == "2026-03-03T00:30:00"
    assert stored_by_value[1000] == "2026-03-03T07:30:00Z"
    assert stored_by_value[1] == "2026-03-02T23:45:00"
    assert {5: "2026-11-01T01:30:00", 7: "2026-11-01T01:30:00"} == {v: stored_by_value[v] for v in (5, 7)}

    assert after_reject == len(READINGS)
