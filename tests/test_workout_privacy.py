"""
Workout sessions and HEALTH_PRIVATE_FIELDS.

A session carries two metrics' values: duration_minutes is the workout_minutes quantity, and the average and maximum
heart rate are the heart_rate metric. read_workout_sessions and log_workout_session used to return them whatever the
privacy setting said, though every other tool nulls, refuses or skips a private metric. These tests check the direct
results of both tools, the analytical result that quotes sessions (explain_metric_change), and that data the setting
does not cover stays available and the stored session is untouched.
"""

import json
import re
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.agent.runner import loaded_server  # noqa: E402

DURATION, AVG_HR, MAX_HR = 137, 151, 183  # distinctive values that occur in no id, date or count below
SESSION = {
    "date": (date.today() - timedelta(days=1)).isoformat(),
    "activity_type": "run",
    "duration_minutes": DURATION,
    "intensity": "high",
    "avg_heart_rate": AVG_HR,
    "max_heart_rate": MAX_HR,
}


def _present(value, text: str) -> bool:
    return re.search(r'(?<!"id": )(?<![\d.])' + re.escape(str(value)) + r"(?![\d])", text) is not None


async def _log_and_read(client, **filters):
    logged = (await client.call_tool("log_workout_session", SESSION)).structured_content["session"]
    read = (await client.call_tool("read_workout_sessions", filters)).structured_content
    return logged, read


async def test_private_workout_minutes_nulls_the_duration_and_keeps_the_rest(tmp_path):
    with loaded_server(tmp_path / "health.db", "workout_minutes") as server:
        async with Client(server.mcp) as client:
            logged, read = await _log_and_read(client)

            for session in (logged, *read["sessions"]):
                assert session["duration_minutes"] is None
                # What the setting does not cover stays available.
                assert (session["activity_type"], session["intensity"]) == ("run", "high")
                assert (session["avg_heart_rate"], session["max_heart_rate"]) == (AVG_HR, MAX_HR)
            assert read["count"] == 1
            assert not _present(DURATION, json.dumps([logged, read]))

            # Filters still work on the redacted rows.
            filtered = (await client.call_tool("read_workout_sessions", {"activity_type": "run"})).structured_content
            assert filtered["count"] == 1 and filtered["sessions"][0]["duration_minutes"] is None
            empty = (await client.call_tool("read_workout_sessions", {"activity_type": "swim"})).structured_content
            assert empty["count"] == 0

        # Redaction is of the output only: the stored session keeps its duration.
        conn = sqlite3.connect(server.HEALTH_DB_PATH)
        try:
            assert conn.execute("SELECT duration_minutes FROM workout_sessions").fetchone() == (DURATION,)
        finally:
            conn.close()


async def test_private_heart_rate_nulls_both_session_heart_rates_and_keeps_the_duration(tmp_path):
    with loaded_server(tmp_path / "health.db", "heart_rate") as server:
        async with Client(server.mcp) as client:
            logged, read = await _log_and_read(client)

            for session in (logged, *read["sessions"]):
                assert (session["avg_heart_rate"], session["max_heart_rate"]) == (None, None)
                assert session["duration_minutes"] == DURATION
            text = json.dumps([logged, read])
            assert not _present(AVG_HR, text) and not _present(MAX_HR, text)


async def test_both_private_nulls_all_three_and_an_unrelated_private_metric_changes_nothing(tmp_path):
    with loaded_server(tmp_path / "both.db", "workout_minutes,heart_rate") as server:
        async with Client(server.mcp) as client:
            logged, read = await _log_and_read(client)
            text = json.dumps([logged, read])
            assert (logged["duration_minutes"], logged["avg_heart_rate"], logged["max_heart_rate"]) == (None,) * 3
            assert not any(_present(v, text) for v in (DURATION, AVG_HR, MAX_HR))

    with loaded_server(tmp_path / "other.db", "weight_kg,water_ml") as server:
        async with Client(server.mcp) as client:
            logged, read = await _log_and_read(client)
            expected = (DURATION, AVG_HR, MAX_HR)
            for session in (logged, *read["sessions"]):
                assert (session["duration_minutes"], session["avg_heart_rate"], session["max_heart_rate"]) == expected


async def test_with_no_private_fields_sessions_are_returned_exactly_as_before(tmp_path):
    with loaded_server(tmp_path / "health.db", "") as server:
        async with Client(server.mcp) as client:
            logged, read = await _log_and_read(client)
    for session in (logged, *read["sessions"]):
        assert (session["duration_minutes"], session["avg_heart_rate"], session["max_heart_rate"]) == (
            DURATION,
            AVG_HR,
            MAX_HR,
        )


async def _log_daily_workout_minutes(client):
    today = date.today()
    for i in range(40):
        day = (today - timedelta(days=i)).isoformat()
        await client.call_tool("log_daily_metric", {"date": day, "workout_minutes": 20 + i % 5, "steps": 5000 + i})


async def test_explain_metric_change_does_not_quote_a_private_heart_rate_from_a_session(tmp_path):
    with loaded_server(tmp_path / "health.db", "heart_rate") as server:
        async with Client(server.mcp) as client:
            await _log_daily_workout_minutes(client)
            await client.call_tool("log_workout_session", SESSION)
            result = await client.call_tool(
                "explain_metric_change", {"metric": "workout_minutes", "date": SESSION["date"]}
            )
    text = json.dumps(result.structured_content, default=str)
    assert result.structured_content["sessions"], "no session was quoted; the test proved nothing"
    assert _present(DURATION, text)  # permitted data is still explained
    assert not _present(AVG_HR, text) and not _present(MAX_HR, text)
    assert "avg HR" not in text


async def test_a_private_workout_minutes_cannot_be_explained_or_analysed_through_sessions(tmp_path):
    with loaded_server(tmp_path / "health.db", "workout_minutes") as server:
        async with Client(server.mcp) as client:
            await _log_daily_workout_minutes(client)
            await client.call_tool("log_workout_session", SESSION)
            outputs = []
            for name, args in [
                ("explain_metric_change", {"metric": "workout_minutes", "date": SESSION["date"]}),
                ("explain_metric_change", {"metric": "steps", "date": SESSION["date"]}),
                ("get_recent_changes", {"days": 14}),
            ]:
                try:
                    outputs.append(json.dumps((await client.call_tool(name, args)).structured_content, default=str))
                except ToolError as exc:
                    outputs.append(f"ERROR {exc}")
    assert outputs[0].startswith("ERROR")  # refused: the private metric is never analysed
    assert not any(_present(DURATION, text) for text in outputs)
    assert not any("workout_minutes" in text for text in outputs[1:])


@pytest.mark.parametrize("private", ["workout_minutes", "heart_rate"])
async def test_the_database_session_row_is_never_modified_by_redaction(tmp_path, private):
    with loaded_server(tmp_path / "health.db", private) as server:
        async with Client(server.mcp) as client:
            await _log_and_read(client)
        conn = sqlite3.connect(server.HEALTH_DB_PATH)
        try:
            row = conn.execute(
                "SELECT duration_minutes, avg_heart_rate, max_heart_rate FROM workout_sessions"
            ).fetchone()
        finally:
            conn.close()
    assert row == (DURATION, AVG_HR, MAX_HR)
