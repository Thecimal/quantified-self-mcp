"""
End to end: a source file -> init_health_db -> measurements -> daily_metrics projection -> MCP tool responses.

Every other import test stops at the database, and every MCP test inserts rows directly. These drive the real
adapters and the real server on small anonymized exports (source names Watch / Phone / Scale, fixed dates) and
check what a client is actually told: observation counts, values, timestamps, provenance, coverage and what an
import run reports. Expected values are worked out by hand in the comments, not read back from the code.
"""

import json
import sys
from pathlib import Path

import pytest
from fastmcp import Client

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from eval.agent.runner import loaded_server  # noqa: E402
from init_db import init_health_db  # noqa: E402

WINDOW = {"start_date": "2026-03-01", "end_date": "2026-03-05"}
LB_TO_KG = 0.45359237


def _quantity(kind, value, unit, when, source, offset="-0800"):
    return (
        f'<Record type="HKQuantityTypeIdentifier{kind}" sourceName="{source}" unit="{unit}" '
        f'startDate="{when} {offset}" endDate="{when} {offset}" value="{value}"/>'
    )


def _sleep(start, end):
    return (
        '<Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Watch" '
        f'startDate="{start} -0800" endDate="{end} -0800" value="HKCategoryValueSleepAnalysisAsleepCore"/>'
    )


# 2026-03-01 and 2026-03-03 have no data at all. Two records are invalid (non-numeric, above the steps bound) and
# one is a record type no metric maps to; none of the three may reach the database.
APPLE_RECORDS = [
    _quantity("StepCount", "3000", "count", "2026-03-02 07:10:00", "Watch"),
    _quantity("StepCount", "4500", "count", "2026-03-02 18:40:00", "Watch"),
    _quantity("StepCount", "900", "count", "2026-03-02 12:00:00", "Phone"),
    _quantity("BodyMass", "154.3", "lb", "2026-03-02 23:30:00", "Scale"),
    _quantity("DietaryWater", "1.5", "L", "2026-03-02 20:00:00", "Phone"),
    _quantity("RestingHeartRate", "58", "count/min", "2026-03-02 06:00:00", "Watch"),
    _quantity("RestingHeartRate", "62", "count/min", "2026-03-02 07:00:00", "Watch"),
    _quantity("HeartRateVariabilitySDNN", "44.0", "ms", "2026-03-02 03:00:00", "Watch"),
    _sleep("2026-03-02 23:00:00", "2026-03-03 07:00:00"),
    _quantity("StepCount", "5000", "count", "2026-03-04 09:00:00", "Watch"),
    _quantity("StepCount", "abc", "count", "2026-03-04 10:00:00", "Watch"),
    _quantity("StepCount", "300000", "count", "2026-03-04 11:00:00", "Watch"),
    _quantity("BloodPressureSystolic", "120", "mmHg", "2026-03-04 08:00:00", "Watch"),
    _quantity("StepCount", "2000", "count", "2026-03-05 08:00:00", "Watch"),
    _quantity("StepCount", "2000", "count", "2026-03-05 20:00:00", "Watch"),
]

# What the database must hold afterwards: (timestamp, metric, value, source). The wall-clock time the source
# reported is kept (no UTC shift); sleep is a day-level total, stored at the noon placeholder under the importer.
EXPECTED_MEASUREMENTS = [
    ("2026-03-02T03:00:00", "hrv_ms", 44.0, "Watch"),
    ("2026-03-02T06:00:00", "resting_heart_rate", 58.0, "Watch"),
    ("2026-03-02T07:00:00", "resting_heart_rate", 62.0, "Watch"),
    ("2026-03-02T07:10:00", "steps", 3000.0, "Watch"),
    ("2026-03-02T12:00:00", "sleep_hours", 8.0, "apple-health"),
    ("2026-03-02T12:00:00", "steps", 900.0, "Phone"),
    ("2026-03-02T18:40:00", "steps", 4500.0, "Watch"),
    ("2026-03-02T20:00:00", "water_ml", 1500.0, "Phone"),
    ("2026-03-02T23:30:00", "weight_kg", 154.3 * LB_TO_KG, "Scale"),
    ("2026-03-04T09:00:00", "steps", 5000.0, "Watch"),
    ("2026-03-05T08:00:00", "steps", 2000.0, "Watch"),
    ("2026-03-05T20:00:00", "steps", 2000.0, "Watch"),
]
# Canonical units after conversion: pounds to kg, litres to ml.
CONVERTED_UNITS = {"steps": "count", "weight_kg": "kg", "water_ml": "ml"}

EXPECTED_DAILY = {
    # steps: Watch 3000 + 4500; Phone's 900 is never added (one source per day, Watch observed more hours).
    "2026-03-02": {
        "steps": 7500,
        "sleep_hours": 8.0,
        "resting_heart_rate": 60,
        "weight_kg": 154.3 * LB_TO_KG,
        "water_ml": 1500,
        "hrv_ms": 44.0,
    },
    "2026-03-04": {"steps": 5000},
    "2026-03-05": {"steps": 4000},
}


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _apple_file(tmp_path, records=APPLE_RECORDS, name="export.xml"):
    return _write(tmp_path, name, '<?xml version="1.0"?><HealthData>' + "".join(records) + "</HealthData>")


def _import(path, db, source="auto"):
    init_health_db(path, db, replace=False, source=source)


async def _call(client, name, arguments=None):
    return (await client.call_tool(name, arguments or {})).structured_content


def _daily(rows):
    return {r["date"]: {k: v for k, v in r.items() if k != "date" and v is not None} for r in rows}


def _approx_rows(actual, expected):
    assert actual.keys() == expected.keys()
    for day, metrics in expected.items():
        assert actual[day].keys() == metrics.keys(), day
        for metric, value in metrics.items():
            assert actual[day][metric] == pytest.approx(value, abs=1e-6), (day, metric)


async def test_every_valid_observation_is_stored_once_with_its_value_timestamp_and_provenance(tmp_path):
    db = tmp_path / "health.db"
    _import(_apple_file(tmp_path), db)
    with loaded_server(db) as server:
        async with Client(server.mcp) as client:
            result = await _call(client, "read_measurements", {**WINDOW, "limit": 100})
    rows = result["measurements"]
    assert result["count"] == len(rows) == len(EXPECTED_MEASUREMENTS)  # the 2 invalid and 1 unmapped records are absent
    actual = sorted((r["timestamp"], r["metric"], r["value"], r["source"]) for r in rows)
    assert [a[:2] + (a[3],) for a in actual] == [e[:2] + (e[3],) for e in sorted(EXPECTED_MEASUREMENTS)]
    assert [a[2] for a in actual] == pytest.approx([e[2] for e in sorted(EXPECTED_MEASUREMENTS)], abs=1e-6)
    for row in rows:
        if row["metric"] in CONVERTED_UNITS:
            assert row["unit"] == CONVERTED_UNITS[row["metric"]], row
        if row["source"] != "apple-health":
            assert row["source_type"] == "wearable", row


async def test_daily_values_coverage_and_window_edges_in_read_health_data(tmp_path):
    db = tmp_path / "health.db"
    _import(_apple_file(tmp_path), db)
    with loaded_server(db) as server:
        async with Client(server.mcp) as client:
            result = await _call(client, "read_health_data", WINDOW)
            last_day = await _call(client, "read_health_data", {"start_date": "2026-03-05", "end_date": "2026-03-05"})
            first_day = await _call(client, "read_health_data", {"start_date": "2026-03-02", "end_date": "2026-03-02"})
    _approx_rows(_daily(result["rows"]), EXPECTED_DAILY)  # no row at all for the two empty days
    coverage = result["coverage"]
    assert (coverage["days_expected"], coverage["days_with_data"], coverage["missing_days"]) == (5, 3, 2)
    assert coverage["coverage_percent"] == 60.0
    assert coverage["metrics"]["steps"] == 60.0
    assert coverage["metrics"]["weight_kg"] == 20.0
    assert coverage["metrics"]["heart_rate"] == 0.0  # never logged: 0 percent, not omitted
    assert result["summary"]["steps"]["avg"] == pytest.approx((7500 + 5000 + 4000) / 3, abs=0.1)
    assert list(_daily(last_day["rows"])) == ["2026-03-05"]  # both edges of a window are inclusive
    assert list(_daily(first_day["rows"])) == ["2026-03-02"]


async def test_missing_days_are_gaps_never_zero_in_history_and_baseline(tmp_path):
    db = tmp_path / "health.db"
    _import(_apple_file(tmp_path), db)
    with loaded_server(db) as server:
        async with Client(server.mcp) as client:
            history = await _call(client, "get_metric_history", {"metric": "steps", **WINDOW})
            baseline = await _call(client, "get_baseline", {"metric": "steps", **WINDOW})
    assert history["points"] == [
        {"date": "2026-03-02", "value": 7500.0},
        {"date": "2026-03-04", "value": 5000.0},
        {"date": "2026-03-05", "value": 4000.0},
    ]
    evidence = history["evidence"]
    assert (evidence["expected_days"], evidence["observed_days"], evidence["missing_days"]) == (5, 3, 2)
    assert evidence["gaps"] == [
        {"start": "2026-03-01", "end": "2026-03-01", "days": 1},
        {"start": "2026-03-03", "end": "2026-03-03", "days": 1},
    ]
    assert "gaps_in_window" in history["data_health"]["reasons"]
    # (7500 + 5000 + 4000) / 3 = 5500 over the days that exist; counting the 2 empty days as 0 would give 3300.
    assert baseline["baseline"]["mean"] == 5500.0
    assert baseline["baseline"]["n"] == 3
    assert baseline["claim"]["decision"]["tier"] == "insufficient"  # 3 days is not enough to call a baseline


async def test_provenance_lists_each_source_with_its_reading_count_and_latest_timestamp(tmp_path):
    db = tmp_path / "health.db"
    _import(_apple_file(tmp_path), db)
    with loaded_server(db) as server:
        async with Client(server.mcp) as client:
            two = await _call(client, "get_metric_provenance", {"metric": "steps", "date": "2026-03-02"})
            one = await _call(client, "get_metric_provenance", {"metric": "steps", "date": "2026-03-04"})
            none = await _call(client, "get_metric_provenance", {"metric": "steps", "date": "2026-03-03"})
    assert [(s["source"], s["n"], s["latest_timestamp"]) for s in two["sources"]] == [
        ("Watch", 2, "2026-03-02T18:40:00"),
        ("Phone", 1, "2026-03-02T12:00:00"),
    ]
    assert two["conflict"] is True  # the Watch day total is 7500 and the Phone's is 900
    assert [(s["source"], s["n"]) for s in one["sources"]] == [("Watch", 1)]
    assert one["conflict"] is False
    assert none["sources"] == [] and none["conflict"] is False


async def test_two_sources_on_one_day_are_reported_not_hidden_from_analytics(tmp_path):
    db = tmp_path / "health.db"
    _import(_apple_file(tmp_path), db)
    with loaded_server(db) as server:
        async with Client(server.mcp) as client:
            history = await _call(client, "get_metric_history", {"metric": "steps", **WINDOW})
            other = await _call(client, "get_metric_history", {"metric": "hrv_ms", **WINDOW})
    assert history["data_health"]["status"] == "MIXED_SOURCES"
    assert "unranked_source_conflict_in_window" in history["data_health"]["reasons"]
    assert "unranked_source_conflict_in_window" not in other["data_health"]["reasons"]  # one source: no flag


async def test_a_repeated_import_changes_nothing_any_tool_reports(tmp_path):
    db = tmp_path / "health.db"
    source = _apple_file(tmp_path)
    snapshots = []
    for _ in range(3):
        _import(source, db)
        with loaded_server(db) as server:
            async with Client(server.mcp) as client:
                history = await _call(client, "get_metric_history", {"metric": "steps", **WINDOW})
                baseline = await _call(client, "get_baseline", {"metric": "steps", **WINDOW})
                snapshots.append(
                    {
                        "rows": (await _call(client, "read_health_data", WINDOW))["rows"],
                        "coverage": (await _call(client, "read_health_data", WINDOW))["coverage"],
                        "measurements": (await _call(client, "read_measurements", {**WINDOW, "limit": 100}))[
                            "measurements"
                        ],
                        "history": {"points": history["points"], "evidence": history["evidence"]},
                        "baseline": {"baseline": baseline["baseline"], "claim": baseline["claim"]},
                    }
                )
    assert snapshots[0] == snapshots[1] == snapshots[2]  # including row ids and created_at: untouched, not rewritten
    with loaded_server(db) as server:
        async with Client(server.mcp) as client:
            runs = (await _call(client, "get_import_status"))["imports"]
    assert len(runs) == 3  # newest first
    assert [(r["records_seen"], r["records_added"], r["records_unchanged"]) for r in runs] == [
        (12, 0, 12),
        (12, 0, 12),
        (12, 12, 0),
    ]
    assert all(r["status"] == "succeeded" and r["rows_skipped"] == 2 for r in runs)  # the 2 invalid records, each time
    assert all(r["rows_loaded"] == 3 for r in runs)  # 3 days of data


async def test_data_status_after_an_import_reports_source_coverage_and_the_gap(tmp_path):
    db = tmp_path / "health.db"
    _import(_apple_file(tmp_path), db)
    with loaded_server(db) as server:
        async with Client(server.mcp) as client:
            status = await _call(client, "get_data_status")
    assert status["status"] == "STALE"  # the newest day is months old
    assert status["latest_data"] == "2026-03-05"
    assert status["coverage"] == {"start": "2026-03-02", "end": "2026-03-05"}
    assert (status["days_with_data"], status["gap_count"], status["missing_days"]) == (3, 1, 1)
    assert status["gaps"] == [{"start": "2026-03-03", "end": "2026-03-03", "days": 1}]
    assert status["source"] == "apple-health"
    assert status["latest_import"]["status"] == "succeeded"


async def test_a_record_stays_on_the_calendar_day_its_source_reported(tmp_path):
    # Midnight edge, with UTC offsets that would move each of these to a different UTC day.
    records = [
        _quantity("StepCount", "100", "count", "2026-03-02 23:59:59", "Watch", offset="-0800"),  # UTC 03-03 07:59
        _quantity("StepCount", "200", "count", "2026-03-03 00:00:00", "Watch", offset="-0800"),  # UTC 03-03 08:00
        _quantity("StepCount", "300", "count", "2026-03-03 00:00:00", "Watch", offset="+0530"),  # UTC 03-02 18:30
    ]
    db = tmp_path / "health.db"
    _import(_apple_file(tmp_path, records), db)
    with loaded_server(db) as server:
        async with Client(server.mcp) as client:
            daily = _daily((await _call(client, "read_health_data", WINDOW))["rows"])
            stored = (await _call(client, "read_measurements", {**WINDOW, "limit": 100}))["measurements"]
    assert daily == {"2026-03-02": {"steps": 100}, "2026-03-03": {"steps": 500}}  # 200 + 300
    assert sorted(r["timestamp"] for r in stored) == [
        "2026-03-02T23:59:59",
        "2026-03-03T00:00:00",
        "2026-03-03T00:00:00",
    ]


def _csv(tmp_path):
    return _write(
        tmp_path,
        "health.csv",
        "date,steps,resting_heart_rate,weight_kg\n2026-03-02,7500,60,69.5\n2026-03-04,5000,,\n",
    )


def _health_connect(tmp_path):
    records = [
        {"recordType": "StepsRecord", "startTime": "2026-03-02T07:10:00Z", "count": 3000},
        {"recordType": "StepsRecord", "startTime": "2026-03-02T18:40:00Z", "count": 4500},
        {"recordType": "RestingHeartRateRecord", "time": "2026-03-02T06:00:00Z", "beatsPerMinute": 58},
        {"recordType": "RestingHeartRateRecord", "time": "2026-03-02T07:00:00Z", "beatsPerMinute": 62},
        {"recordType": "WeightRecord", "time": "2026-03-02T06:00:00Z", "weight": {"value": 70.0, "unit": "kg"}},
        {"recordType": "WeightRecord", "time": "2026-03-02T21:00:00Z", "weight": {"value": 69.5, "unit": "kg"}},
        {"recordType": "StepsRecord", "startTime": "2026-03-04T09:00:00Z", "count": 5000},
    ]
    return _write(tmp_path, "export.json", json.dumps(records))


def _apple_same_week(tmp_path):
    records = [
        _quantity("StepCount", "3000", "count", "2026-03-02 07:10:00", "Watch", offset="+0000"),
        _quantity("StepCount", "4500", "count", "2026-03-02 18:40:00", "Watch", offset="+0000"),
        _quantity("RestingHeartRate", "58", "count/min", "2026-03-02 06:00:00", "Watch", offset="+0000"),
        _quantity("RestingHeartRate", "62", "count/min", "2026-03-02 07:00:00", "Watch", offset="+0000"),
        _quantity("BodyMass", "70.0", "kg", "2026-03-02 06:00:00", "Scale", offset="+0000"),
        _quantity("BodyMass", "69.5", "kg", "2026-03-02 21:00:00", "Scale", offset="+0000"),
        _quantity("StepCount", "5000", "count", "2026-03-04 09:00:00", "Watch", offset="+0000"),
    ]
    return _apple_file(tmp_path, records, "same_week.xml")


@pytest.mark.parametrize(
    ("make", "source"),
    [(_csv, "csv"), (_health_connect, "health-connect"), (_apple_same_week, "apple-health")],
)
async def test_one_logical_week_reads_the_same_whichever_format_it_arrived_in(tmp_path, make, source):
    db = tmp_path / "health.db"
    _import(make(tmp_path), db, source=source)
    with loaded_server(db) as server:
        async with Client(server.mcp) as client:
            result = await _call(client, "read_health_data", WINDOW)
    # steps are summed, resting heart rate is the mean of 58 and 62, weight is the latest reading of the day.
    assert _daily(result["rows"]) == {
        "2026-03-02": {"steps": 7500, "resting_heart_rate": 60, "weight_kg": 69.5},
        "2026-03-04": {"steps": 5000},
    }


async def test_csv_blank_cells_are_missing_and_an_explicit_zero_is_a_real_value(tmp_path):
    path = _write(
        tmp_path,
        "health.csv",
        "date,steps,weight_kg\n2026-03-02,1000,70.5\n2026-03-03,,70.0\n2026-03-04,0,\n",
    )
    db = tmp_path / "health.db"
    _import(path, db, source="csv")
    with loaded_server(db) as server:
        async with Client(server.mcp) as client:
            rows = (await _call(client, "read_health_data", WINDOW))["rows"]
            steps = await _call(client, "get_metric_history", {"metric": "steps", **WINDOW})
    by_day = {r["date"]: r for r in rows}
    assert by_day["2026-03-03"]["steps"] is None  # blank: not logged, so not 0
    assert by_day["2026-03-04"]["steps"] == 0  # a logged zero is data
    assert by_day["2026-03-04"]["weight_kg"] is None
    assert [p["date"] for p in steps["points"]] == ["2026-03-02", "2026-03-04"]
    assert steps["evidence"]["observed_days"] == 2


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT: get_metric_provenance compares each source's AVERAGE reading (documented), which for a "
    "sum metric such as steps is not the source's day total, so two sources that agree on the day total are "
    "reported as conflicting. Fixing it changes a documented field, so it waits for a decision.",
)
async def test_sources_that_agree_on_the_day_total_are_not_reported_as_a_conflicting_reading(tmp_path):
    # Watch logs 1000 steps as two records, Phone as one: both say 1000 for the day.
    records = [
        _quantity("StepCount", "500", "count", "2026-03-02 08:00:00", "Watch"),
        _quantity("StepCount", "500", "count", "2026-03-02 09:00:00", "Watch"),
        _quantity("StepCount", "1000", "count", "2026-03-02 10:00:00", "Phone"),
    ]
    db = tmp_path / "health.db"
    _import(_apple_file(tmp_path, records), db)
    with loaded_server(db) as server:
        async with Client(server.mcp) as client:
            provenance = await _call(client, "get_metric_provenance", {"metric": "steps", "date": "2026-03-02"})
    assert {s["source"] for s in provenance["sources"]} == {"Watch", "Phone"}
    assert provenance["conflict"] is False


async def test_read_measurements_for_a_single_day_returns_every_reading_on_it(tmp_path):
    db = tmp_path / "health.db"
    _import(_apple_file(tmp_path), db)
    with loaded_server(db) as server:
        async with Client(server.mcp) as client:
            last = await _call(
                client, "read_measurements", {"metric": "steps", "start_date": "2026-03-05", "end_date": "2026-03-05"}
            )
            before = await _call(client, "read_measurements", {"metric": "steps", "end_date": "2026-03-04"})
    assert sorted((r["timestamp"], r["value"]) for r in last["measurements"]) == [
        ("2026-03-05T08:00:00", 2000.0),
        ("2026-03-05T20:00:00", 2000.0),
    ]
    assert max(r["timestamp"] for r in before["measurements"]) == "2026-03-04T09:00:00"  # the 05th is excluded


def _query_conn(tmp_path):
    from logic import connect_writable, ensure_schema, insert_measurement

    conn = connect_writable(tmp_path / "health.db")
    ensure_schema(conn)
    for stamp in ("2026-06-01T23:59:59", "2026-06-02T00:00:00", "2026-06-02T08:00:00", "2026-06-02T23:59:59"):
        insert_measurement(conn, stamp, "steps", 1, source="Watch")
    insert_measurement(conn, "2026-06-03T00:00:00", "steps", 1, source="Watch")
    return conn


def test_a_plain_date_end_includes_that_whole_day_and_nothing_after_it(tmp_path):
    from logic import query_measurements

    conn = _query_conn(tmp_path)
    got = sorted(r["timestamp"] for r in query_measurements(conn, start="2026-06-02", end="2026-06-02"))
    assert got == ["2026-06-02T00:00:00", "2026-06-02T08:00:00", "2026-06-02T23:59:59"]


def test_a_full_timestamp_end_is_still_an_exact_bound(tmp_path):
    from logic import query_measurements

    conn = _query_conn(tmp_path)
    got = sorted(r["timestamp"] for r in query_measurements(conn, start="2026-06-02", end="2026-06-02T08:00:00"))
    assert got == ["2026-06-02T00:00:00", "2026-06-02T08:00:00"]


@pytest.mark.parametrize("end", ["9999-12-31", "2026-13-45", "2026-6-2", "not-a-date", ""])
def test_an_unusual_end_never_raises(tmp_path, end):
    from logic import query_measurements

    query_measurements(_query_conn(tmp_path), end=end)
