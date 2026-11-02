"""
Calendar-day boundaries: the conflict count must bucket days the way the daily view does.

Every timestamp in `measurements` is naive local wall-clock time, and daily_metrics buckets a reading with SQLite
date(timestamp): every reading from 00:00:00 through 23:59:59.999 is that day, and 00:00:00 the next day is the
next (SQLite rounds finer fractions, so these tests stay at millisecond precision). count_source_conflicts (the
"conflicting_days" caveat of explain_metric_change) bounded its window with "<last day>T23:59:59", so a reading in
the last second of the window's last day that carries fractional seconds (".5", ".500000": both allowed by the ingest
contract, and WHOOP imports emit microseconds) fell outside the window although the daily view had put it on that
day. These tests pin the boundary on both sides and through the tool.
"""

import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastmcp import Client

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.agent.runner import loaded_server  # noqa: E402
from logic import connect_writable, count_source_conflicts, ensure_schema, import_measurements  # noqa: E402

DAY = date(2026, 6, 1)


def _db(tmp_path, rows):
    conn = connect_writable(tmp_path / "health.db")
    ensure_schema(conn)
    import_measurements(conn, "test", [{"metric": "steps", **row} for row in rows])
    return conn


def _sources_on(conn, day):
    row = conn.execute("SELECT source_count FROM daily_metrics WHERE metric = 'steps' AND date = ?", (day,)).fetchone()
    return row[0] if row else 0


@pytest.mark.parametrize(
    "last_second",
    ["2026-06-01T23:59:59", "2026-06-01T23:59:59.500", "2026-06-01T23:59:59.500000", "2026-06-01T23:59:59.999"],
)
def test_a_reading_in_the_last_second_of_the_last_day_still_counts_toward_a_conflict(tmp_path, last_second):
    conn = _db(
        tmp_path,
        [
            {"timestamp": "2026-06-01T12:00:00", "value": 100, "source": "Watch"},
            {"timestamp": last_second, "value": 200, "source": "Phone"},
        ],
    )
    try:
        assert _sources_on(conn, "2026-06-01") == 2  # the daily view puts both on the day
        assert count_source_conflicts(conn, "steps", DAY, DAY) == 1  # so the conflict is counted, whatever the format
    finally:
        conn.close()


def test_midnight_belongs_to_the_next_day_on_both_sides_of_the_boundary(tmp_path):
    conn = _db(
        tmp_path,
        [
            {"timestamp": "2026-06-01T12:00:00", "value": 100, "source": "Watch"},
            {"timestamp": "2026-06-02T00:00:00", "value": 200, "source": "Phone"},
            {"timestamp": "2026-05-31T23:59:59.999", "value": 300, "source": "Scale"},
        ],
    )
    try:
        # Each reading is alone on its own day, so the daily view sees one source per day and nothing conflicts.
        assert [_sources_on(conn, d) for d in ("2026-05-31", "2026-06-01", "2026-06-02")] == [1, 1, 1]
        # The window's edges do not pull a neighbouring day's reading in.
        assert count_source_conflicts(conn, "steps", DAY, DAY) == 0
        assert count_source_conflicts(conn, "steps", DAY - timedelta(days=1), DAY + timedelta(days=1)) == 0
    finally:
        conn.close()


def test_each_conflicting_day_is_counted_once_and_a_repeated_single_source_is_not_a_conflict(tmp_path):
    conn = _db(
        tmp_path,
        [
            # 05-31: two sources, one of them in the last second -> a conflict.
            {"timestamp": "2026-05-31T08:00:00", "value": 100, "source": "Watch"},
            {"timestamp": "2026-05-31T23:59:59.250", "value": 100, "source": "Phone"},
            # 06-01: the same source twice -> not a conflict.
            {"timestamp": "2026-06-01T08:00:00", "value": 100, "source": "Watch"},
            {"timestamp": "2026-06-01T23:59:59.250", "value": 100, "source": "Watch"},
            # 06-02: two sources at ordinary times -> a conflict, counted as before.
            {"timestamp": "2026-06-02T08:00:00", "value": 100, "source": "Watch"},
            {"timestamp": "2026-06-02T09:00:00", "value": 100, "source": "Phone"},
        ],
    )
    try:
        assert count_source_conflicts(conn, "steps", date(2026, 5, 31), date(2026, 6, 2)) == 2
        assert count_source_conflicts(conn, "steps", date(2026, 5, 31), date(2026, 6, 1)) == 1
        assert count_source_conflicts(conn, "steps", date(2026, 6, 1), date(2026, 6, 2)) == 1
    finally:
        conn.close()


async def test_explain_metric_change_reports_a_conflict_that_sits_in_the_last_second_of_the_day(tmp_path):
    target = date.today() - timedelta(days=1)
    rows = [
        {"timestamp": f"{(target - timedelta(days=i)).isoformat()}T08:00:00", "value": 5000 + i, "source": "Watch"}
        for i in range(40)
    ]
    rows.append({"timestamp": f"{target.isoformat()}T23:59:59.500", "value": 5200, "source": "Phone"})
    conn = _db(tmp_path, rows)
    conn.close()

    with loaded_server(tmp_path / "health.db", "") as server:
        async with Client(server.mcp) as client:
            result = (
                await client.call_tool("explain_metric_change", {"metric": "steps", "date": target.isoformat()})
            ).structured_content

    assert result["conflicting_days"] == 1
    assert any("conflicting values from different sources on 1 of" in fact for fact in result["narrative_facts"])
    check = sqlite3.connect(tmp_path / "health.db")
    try:
        assert _sources_on(check, target.isoformat()) == 2
    finally:
        check.close()
