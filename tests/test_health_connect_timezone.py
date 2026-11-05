"""
Health Connect import: records are filed under the user's local calendar day.

A Health Connect timestamp is an instant, usually written in UTC ("Z"). The importer used to take its date straight from
that string, so at UTC+02:00 a step count at 00:30 local (22:30Z) landed on the previous day. Records carry their own
zone offset (startZoneOffset on interval records, zoneOffset on instantaneous ones); the instant is now converted to it
once, then the date is taken. A missing or malformed offset, or a timestamp with no offset, behaves as before.
These tests run the real import (file -> adapter -> measurements -> daily_metrics).
"""

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from init_db import init_health_db  # noqa: E402


def _steps(start, count=100, **extra):
    return {"recordType": "StepsRecord", "startTime": start, "endTime": start, "count": count, **extra}


def _daily(tmp_path, records, metric="steps"):
    path = tmp_path / "hc.json"
    path.write_text(json.dumps(records), encoding="utf-8")
    db = tmp_path / "health.db"
    init_health_db(path, db, replace=False, source="health-connect")
    conn = sqlite3.connect(db)
    try:
        return dict(conn.execute("SELECT date, value FROM daily_metrics WHERE metric = ?", (metric,)).fetchall())
    finally:
        conn.close()


def test_late_evening_local_time_stays_on_its_local_day_east_of_utc(tmp_path, capsys):
    # 23:30 at UTC+02:00 is 21:30Z: same date either way.
    assert _daily(tmp_path, [_steps("2026-06-01T21:30:00Z", startZoneOffset="+02:00")]) == {"2026-06-01": 100.0}


def test_after_local_midnight_moves_to_the_next_local_day_when_utc_is_still_the_previous_day(tmp_path, capsys):
    # 00:30 local on 06-02 at UTC+02:00 is 22:30Z on 06-01.
    assert _daily(tmp_path, [_steps("2026-06-01T22:30:00Z", startZoneOffset="+02:00")]) == {"2026-06-02": 100.0}


def test_west_of_utc_an_evening_reading_stays_on_its_local_day(tmp_path, capsys):
    # 21:00 local on 06-01 at UTC-08:00 is 05:00Z on 06-02.
    assert _daily(tmp_path, [_steps("2026-06-02T05:00:00Z", startZoneOffset="-08:00")]) == {"2026-06-01": 100.0}


def test_equivalent_instants_in_different_offsets_land_on_the_same_local_day(tmp_path, capsys):
    records = [
        _steps("2026-06-01T22:30:00Z", 100, startZoneOffset="+02:00"),
        _steps("2026-06-02T00:30:00+02:00", 10, startZoneOffset="+02:00"),  # same instant, written in local time
    ]
    assert _daily(tmp_path, records) == {"2026-06-02": 110.0}


def test_the_record_offset_is_applied_once_not_twice_when_the_string_already_carries_it(tmp_path, capsys):
    # 00:30+02:00 is already local; converting to +02:00 again must not shift it.
    assert _daily(tmp_path, [_steps("2026-06-02T00:30:00+02:00", startZoneOffset="+02:00")]) == {"2026-06-02": 100.0}


def test_the_record_offset_wins_over_a_different_offset_in_the_timestamp_string(tmp_path, capsys):
    # The instant is 22:30Z; the record says the user was at +02:00, whatever the string's own offset.
    assert _daily(tmp_path, [_steps("2026-06-01T23:30:00+01:00", startZoneOffset="+02:00")]) == {"2026-06-02": 100.0}


def test_half_hour_and_seconds_offsets_are_understood(tmp_path, capsys):
    # +05:30: 19:00Z is 00:30 next day.
    assert _daily(tmp_path, [_steps("2026-06-01T19:00:00Z", startZoneOffset="+05:30")]) == {"2026-06-02": 100.0}
    other = tmp_path / "b"
    other.mkdir()
    # +02:00:01 (seconds are allowed): 21:59:59Z is 00:00:00 next day.
    assert _daily(other, [_steps("2026-06-01T21:59:59Z", startZoneOffset="+02:00:01")]) == {"2026-06-02": 100.0}


def test_missing_or_invalid_offsets_fall_back_to_the_timestamps_own_date(tmp_path, capsys):
    for i, extra in enumerate(
        [
            {},
            {"startZoneOffset": None},
            {"startZoneOffset": "Europe/Berlin"},
            {"startZoneOffset": "+25:00"},
            {"startZoneOffset": "+02:99"},
            {"startZoneOffset": 7200},
            {"startZoneOffset": {"totalSeconds": 7200}},
            {"startZoneOffset": ""},
        ]
    ):
        sub = tmp_path / str(i)
        sub.mkdir()
        assert _daily(sub, [_steps("2026-06-01T22:30:00Z", **extra)]) == {"2026-06-01": 100.0}, extra


def test_a_timestamp_with_no_offset_is_wall_clock_and_is_not_converted(tmp_path, capsys):
    assert _daily(tmp_path, [_steps("2026-06-01T23:30:00", startZoneOffset="+02:00")]) == {"2026-06-01": 100.0}


def test_instantaneous_records_use_zone_offset(tmp_path, capsys):
    rec = {"recordType": "WeightRecord", "time": "2026-06-01T22:30:00Z", "weight": {"value": 70, "unit": "kilograms"}}
    assert _daily(tmp_path, [{**rec, "zoneOffset": "+02:00"}], "weight_kg") == {"2026-06-02": 70.0}
    other = tmp_path / "b"
    other.mkdir()
    assert _daily(other, [{**rec, "startZoneOffset": "+02:00"}], "weight_kg") == {
        "2026-06-01": 70.0
    }  # wrong field: ignored
    for metric, record in [
        (
            "resting_heart_rate",
            {"recordType": "RestingHeartRateRecord", "time": "2026-06-01T22:30:00Z", "beatsPerMinute": 55},
        ),
        (
            "hrv_ms",
            {
                "recordType": "HeartRateVariabilityRmssdRecord",
                "time": "2026-06-01T22:30:00Z",
                "heartRateVariabilityMillis": 40,
            },
        ),
    ]:
        sub = tmp_path / metric
        sub.mkdir()
        assert list(_daily(sub, [{**record, "zoneOffset": "+02:00"}], metric)) == ["2026-06-02"]


def test_heart_rate_samples_use_the_record_start_offset(tmp_path, capsys):
    record = {
        "recordType": "HeartRateRecord",
        "startTime": "2026-06-01T22:00:00Z",
        "endTime": "2026-06-01T23:00:00Z",
        "startZoneOffset": "+02:00",
        "samples": [{"time": "2026-06-01T22:30:00Z", "beatsPerMinute": 60}],
    }
    assert _daily(tmp_path, [record], "heart_rate") == {"2026-06-02": 60.0}


def test_exercise_and_sleep_are_filed_on_the_local_start_day(tmp_path, capsys):
    exercise = {
        "recordType": "ExerciseSessionRecord",
        "startTime": "2026-06-01T22:30:00Z",
        "endTime": "2026-06-01T23:30:00Z",
        "startZoneOffset": "+02:00",
        "endZoneOffset": "+02:00",
    }
    assert _daily(tmp_path, [exercise], "workout_minutes") == {"2026-06-02": 60.0}
    sleep = {
        "recordType": "SleepSessionRecord",
        "startTime": "2026-06-01T22:30:00Z",
        "endTime": "2026-06-02T05:30:00Z",
        "startZoneOffset": "+02:00",
        "endZoneOffset": "+02:00",
    }
    other = tmp_path / "s"
    other.mkdir()
    assert _daily(other, [sleep], "sleep_hours") == {"2026-06-02": 7.0}
