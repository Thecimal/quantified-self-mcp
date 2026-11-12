"""
get_data_status: a metric that stopped arriving is reported even while the database as a whole is current.

The status and latest_data came from the newest day of *any* metric, so steps logged daily kept the answer CURRENT
while HRV had not been updated for weeks, and a client had nothing to tell it so. stale_metrics lists each metric whose
own latest day trails today by more than its threshold (daily metrics: stale_after_days; weight and workouts, which are
not expected daily: 30 days). It is about the day a value is for, never when it was imported. The tool reads
date.today(), so every date here is relative to it.
"""

import sys
from datetime import date, timedelta
from pathlib import Path

from fastmcp import Client

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.agent.runner import loaded_server  # noqa: E402
from init_db import init_health_db  # noqa: E402
from logic import connect_writable, ensure_schema  # noqa: E402
from source_coverage import stale_metrics  # noqa: E402


def _day(age: int) -> str:
    return (date.today() - timedelta(days=age)).isoformat()


async def _log(client, age: int, **metrics) -> None:
    await client.call_tool("log_daily_metric", {"date": _day(age), **metrics})


async def _status(client, **args):
    return (await client.call_tool("get_data_status", args)).structured_content


async def _steps_every_day_hrv_stopped(client, hrv_last_age: int) -> None:
    for age in range(0, 11):
        await _log(client, age, steps=8000, **({"hrv_ms": 55} if age >= hrv_last_age else {}))


async def test_a_metric_that_stopped_arriving_is_listed_while_the_dataset_is_current(tmp_path):
    with loaded_server(tmp_path / "health.db") as server:
        async with Client(server.mcp) as client:
            await _steps_every_day_hrv_stopped(client, hrv_last_age=6)
            status = await _status(client)

    assert (status["status"], status["latest_data"], status["days_behind"]) == ("CURRENT", _day(0), 0)  # unchanged
    assert status["stale_metrics"] == [
        {"metric": "hrv_ms", "latest_data": _day(6), "days_behind": 6, "stale_after_days": 2}
    ]


async def test_everything_recent_lists_nothing(tmp_path):
    with loaded_server(tmp_path / "health.db") as server:
        async with Client(server.mcp) as client:
            await _log(client, 0, steps=8000, hrv_ms=55)
            await _log(client, 1, steps=7000, hrv_ms=50)
            status = await _status(client)
    assert status["status"] == "CURRENT"
    assert status["stale_metrics"] == []


async def test_metrics_with_different_update_frequencies_use_their_own_thresholds(tmp_path):
    with loaded_server(tmp_path / "health.db") as server:
        async with Client(server.mcp) as client:
            await _log(client, 0, steps=8000)
            await _log(client, 3, hrv_ms=55)  # daily metric, 3 days behind: stale at the default of 2
            await _log(client, 20, weight_kg=80.0)  # episodic, 20 days behind: not expected daily
            await _log(client, 31, workout_minutes=40)  # episodic, 31 days behind: past 30
            default = await _status(client)
            lenient = await _status(client, stale_after_days=7)

    assert [(m["metric"], m["days_behind"], m["stale_after_days"]) for m in default["stale_metrics"]] == [
        ("workout_minutes", 31, 30),
        ("hrv_ms", 3, 2),
    ]  # most behind first
    assert [m["metric"] for m in lenient["stale_metrics"]] == ["workout_minutes"]  # hrv is within 7 days


async def test_old_measurements_imported_today_are_still_stale(tmp_path):
    # A backfill imported just now carries old measurement dates: the import time must not make them look current.
    db = tmp_path / "health.db"
    csv = tmp_path / "backfill.csv"
    csv.write_text(
        f"date,steps,hrv_ms\n{_day(0)},8000,\n{_day(1)},7000,\n{_day(9)},6000,50\n{_day(8)},6500,52\n",
        encoding="utf-8",
    )
    init_health_db(csv, db, replace=False)

    with loaded_server(db) as server:
        async with Client(server.mcp) as client:
            status = await _status(client)

    assert status["last_successful_import"] is not None
    assert status["last_successful_import"][:10] == date.today().isoformat()  # imported today...
    assert status["stale_metrics"] == [
        {
            "metric": "hrv_ms",
            "latest_data": _day(8),
            "days_behind": 8,
            "stale_after_days": 2,
        }  # ...but recorded 8 days ago
    ]


async def test_missing_import_and_provenance_metadata_changes_nothing(tmp_path):
    # Rows logged by hand have no importer or imported_at, and there is no import history at all.
    with loaded_server(tmp_path / "health.db") as server:
        async with Client(server.mcp) as client:
            await _steps_every_day_hrv_stopped(client, hrv_last_age=5)
            status = await _status(client)
    assert status["latest_import"] is None and status["last_successful_import"] is None
    assert [m["metric"] for m in status["stale_metrics"]] == ["hrv_ms"]


async def test_an_empty_database_lists_nothing(tmp_path):
    with loaded_server(tmp_path / "health.db") as server:
        async with Client(server.mcp) as client:
            status = await _status(client)
    assert status["status"] == "NO_DATA"
    assert status["stale_metrics"] == []


async def test_private_metrics_are_not_listed(tmp_path):
    with loaded_server(tmp_path / "health.db", private_fields="hrv_ms") as server:
        async with Client(server.mcp) as client:
            await _steps_every_day_hrv_stopped(client, hrv_last_age=6)
            status = await _status(client)
    assert status["stale_metrics"] == []
    assert "hrv_ms" not in str(status)


def test_a_future_dated_metric_is_not_stale_and_the_rest_are_listed_most_behind_first(tmp_path):
    conn = connect_writable(tmp_path / "health.db")
    ensure_schema(conn)
    today = date(2026, 10, 3)
    for metric, day in (("steps", "2026-09-20"), ("hrv_ms", "2026-09-25"), ("heart_rate", "2026-10-09")):
        conn.execute(
            "INSERT INTO measurements (timestamp, metric, value, source) VALUES (?, ?, 1, 'daily-log')",
            (f"{day}T12:00:00", metric),
        )
    conn.commit()
    assert [(m["metric"], m["days_behind"]) for m in stale_metrics(conn, today)] == [("steps", 13), ("hrv_ms", 8)]
    conn.close()
