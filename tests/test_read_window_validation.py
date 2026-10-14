"""
Input contract of the two raw-row read tools, read_measurements and read_workout_sessions.

An unparseable date, a window that ends before it starts, or a limit outside 1..1000 used to come back as a
successful empty list (or an unbounded one for limit=-1 and for an unparseable end_date). A caller reads that as
"no data" and reasons from it. The sibling read_health_data already answers such input with [invalid_range];
these tests hold the two raw-row tools to the same contract and check valid calls are unchanged.
"""

import sys
from pathlib import Path

import pytest
from fastmcp import Client

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from eval.agent.runner import loaded_server  # noqa: E402
from logic import connect_writable, ensure_schema  # noqa: E402

BAD_DATES = ["last week", "yesterday", "2026-3-5", "2026-02-30", "2026/03/05", "20260305", "garbage"]


@pytest.fixture
async def client(tmp_path):
    db = tmp_path / "health.db"
    conn = connect_writable(db)
    ensure_schema(conn)
    conn.close()
    with loaded_server(db) as server:
        async with Client(server.mcp) as c:
            for day, value in (("2026-03-01", 100), ("2026-03-02", 200), ("2026-03-03", 300)):
                await c.call_tool(
                    "log_measurement",
                    {"timestamp": f"{day}T09:00:00", "metric": "steps", "value": value, "source": "Watch"},
                )
                await c.call_tool(
                    "log_workout_session", {"date": day, "activity_type": "running", "duration_minutes": 30}
                )
            yield c


async def _read(client, tool, **args):
    return await client.call_tool(tool, args, raise_on_error=False)


def _assert_invalid_range(result, fragment):
    assert result.is_error, result.structured_content
    text = result.content[0].text
    assert "[invalid_range]" in text and fragment in text, text


@pytest.mark.parametrize("tool", ["read_measurements", "read_workout_sessions"])
@pytest.mark.parametrize("field", ["start_date", "end_date"])
@pytest.mark.parametrize("bad", BAD_DATES)
async def test_a_date_that_is_not_yyyy_mm_dd_is_an_invalid_range_error_not_an_empty_success(client, tool, field, bad):
    _assert_invalid_range(await _read(client, tool, **{field: bad}), field)


@pytest.mark.parametrize("tool", ["read_measurements", "read_workout_sessions"])
async def test_a_window_ending_before_it_starts_is_an_error_not_an_empty_success(client, tool):
    _assert_invalid_range(await _read(client, tool, start_date="2026-03-03", end_date="2026-03-01"), "after")


@pytest.mark.parametrize("tool", ["read_measurements", "read_workout_sessions"])
@pytest.mark.parametrize("limit", [0, -1, 1001, 10**9])
async def test_a_limit_outside_1_to_1000_is_an_error_not_an_unbounded_or_empty_success(client, tool, limit):
    _assert_invalid_range(await _read(client, tool, limit=limit), "limit")


async def test_valid_windows_and_limits_return_exactly_what_they_did_before(client):
    got = await _read(client, "read_measurements", start_date="2026-03-02", end_date="2026-03-02")
    assert [m["value"] for m in got.structured_content["measurements"]] == [200]
    got = await _read(client, "read_workout_sessions", start_date="2026-03-02", end_date="2026-03-03", limit=1000)
    assert [s["date"] for s in got.structured_content["sessions"]] == ["2026-03-03", "2026-03-02"]
    got = await _read(client, "read_measurements", limit=1)
    assert [m["value"] for m in got.structured_content["measurements"]] == [300]
    # a window with no data is still a legitimate empty success, distinct from invalid input
    got = await _read(client, "read_measurements", start_date="2027-01-01", end_date="2027-01-31")
    assert not got.is_error and got.structured_content["count"] == 0


async def test_read_measurements_still_accepts_iso_timestamps_and_empty_strings_as_bounds(client):
    got = await _read(client, "read_measurements", start_date="2026-03-02T00:00:00", end_date="2026-03-02T23:59:59")
    assert [m["value"] for m in got.structured_content["measurements"]] == [200]
    got = await _read(client, "read_measurements", start_date="", end_date="")
    assert got.structured_content["count"] == 3
    _assert_invalid_range(await _read(client, "read_measurements", start_date="2026-03-02Tgarbage"), "start_date")
