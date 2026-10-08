"""
Canonical ingestion contract (ingest.py): what an adapter's record must look like, what the validator
normalises, and what it rejects. Pure unit tests apart from the last one, which feeds contract rows through
the real import path to show the contract is additive.
"""

import math
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingest import (  # noqa: E402
    CanonicalRecord,
    RecordError,
    drop_duplicate_ids,
    normalize_batch,
    normalize_record,
)
from logic import connect_writable, ensure_schema, import_measurements  # noqa: E402
from metric_registry import METRICS  # noqa: E402

TS = "2026-08-10T12:00:00"


def _raw(**overrides):
    return {"metric": "steps", "timestamp": TS, "value": 8000, **overrides}


def _rejects(match, **overrides):
    with pytest.raises(RecordError, match=match):
        normalize_record(_raw(**overrides))


def test_a_minimal_record_is_valid_and_gets_the_registry_unit():
    record = normalize_record(_raw())
    assert record == CanonicalRecord("steps", TS, 8000, unit="count")
    assert record.to_row() == {
        "metric": "steps",
        "timestamp": TS,
        "value": 8000,
        "unit": "count",
        "source": None,
        "source_type": None,
        "source_record_id": None,
        "timezone": None,
        "quality": None,
    }


def test_every_field_is_carried_through():
    record = normalize_record(
        _raw(
            metric="heart_rate",
            value=61.5,
            unit="count/min",
            source=" Ben's Apple Watch ",
            source_type="wearable",
            source_record_id="abc-123",
            timezone="+01:00",
            quality=0.8,
        )
    )
    assert record.to_row() == {
        "metric": "heart_rate",
        "timestamp": TS,
        "value": 61.5,
        "unit": "bpm",
        "source": "Ben's Apple Watch",
        "source_type": "wearable",
        "source_record_id": "abc-123",
        "timezone": "+01:00",
        "quality": 0.8,
    }


def test_records_are_immutable():
    record = normalize_record(_raw())
    with pytest.raises(FrozenInstanceError):
        record.value = 1  # type: ignore[misc]


@pytest.mark.parametrize("metric", list(METRICS))
def test_every_registry_metric_accepts_its_own_canonical_unit(metric):
    definition = METRICS[metric]
    value = definition.min_value if definition.min_value > 0 else 1
    record = normalize_record(_raw(metric=metric, value=value, unit=definition.unit))
    assert record.unit == definition.unit


@pytest.mark.parametrize(
    ("metric", "given", "canonical"),
    [
        ("resting_heart_rate", "count/min", "bpm"),
        ("heart_rate", "BPM", "bpm"),
        ("water_ml", "ml", "mL"),
        ("water_ml", "mL", "mL"),
        ("sleep_hours", "hours", "h"),
        ("workout_minutes", "Minutes", "min"),
        ("weight_kg", "kilograms", "kg"),
        ("hrv_ms", "MS", "ms"),
        ("steps", "counts", "count"),
    ],
)
def test_known_unit_spellings_are_normalised(metric, given, canonical):
    low = METRICS[metric].min_value
    assert normalize_record(_raw(metric=metric, value=max(low, 1), unit=given)).unit == canonical


@pytest.mark.parametrize(
    ("metric", "value", "unit"),
    [
        ("weight_kg", 70, "lb"),
        ("water_ml", 500, "L"),
        ("steps", 5, "kg"),
        ("heart_rate", 60, "mmHg"),
        ("mood", 5, "points"),
    ],
)
def test_a_unit_that_needs_a_conversion_or_does_not_belong_is_rejected(metric, value, unit):
    with pytest.raises(RecordError, match="must be in|has no unit"):
        normalize_record(_raw(metric=metric, value=value, unit=unit))


def test_a_metric_without_a_unit_stays_without_one():
    assert normalize_record(_raw(metric="mood", value=7)).unit is None
    assert normalize_record(_raw(metric="mood", value=7, unit="  ")).unit is None


@pytest.mark.parametrize("timestamp", [None, "", "   "])
def test_a_missing_timestamp_is_rejected(timestamp):
    _rejects("missing timestamp|missing", timestamp=timestamp)


def test_a_record_with_no_timestamp_key_is_rejected():
    raw = _raw()
    del raw["timestamp"]
    with pytest.raises(RecordError, match="missing timestamp"):
        normalize_record(raw)


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-08-10",
        "2026-08-10 12:00:00",
        "2026-08-10T12:00",
        "2026-08-10T12:00:00Z",
        "2026-08-10T12:00:00+01:00",
        "2026-08-10T12:00:00-08:00",
        "2026-08-10T12:00:00.5",
        "10/08/2026",
        "yesterday",
    ],
)
def test_timestamps_must_be_naive_local_datetimes(timestamp):
    _rejects("timestamp", timestamp=timestamp)


@pytest.mark.parametrize("timestamp", ["2026-13-10T12:00:00", "2026-02-30T12:00:00", "2026-08-10T25:00:00"])
def test_impossible_dates_and_times_are_rejected(timestamp):
    _rejects("not a real date", timestamp=timestamp)


@pytest.mark.parametrize(
    "timestamp", [TS, "2026-08-10T23:59:59", "2026-08-10T12:00:00.250", "2026-08-10T12:00:00.250000"]
)
def test_naive_timestamps_with_optional_fractions_are_accepted(timestamp):
    assert normalize_record(_raw(timestamp=timestamp)).timestamp == timestamp


def test_a_timestamp_at_a_timezone_boundary_keeps_its_wall_clock_day():
    record = normalize_record(_raw(timestamp="2026-01-15T23:30:00", timezone="-08:00"))
    assert (record.timestamp, record.timezone) == ("2026-01-15T23:30:00", "-08:00")


@pytest.mark.parametrize("value", [None, "12", "", [], {}, True, False, math.nan, math.inf, -math.inf])
def test_values_must_be_finite_numbers(value):
    with pytest.raises(RecordError):
        normalize_record(_raw(value=value))


@pytest.mark.parametrize(("metric", "value"), [("steps", -1), ("steps", 200_001), ("mood", 11), ("heart_rate", 5)])
def test_values_outside_the_registry_bounds_are_rejected(metric, value):
    _rejects("must be between", metric=metric, value=value)


def test_the_bounds_are_inclusive():
    assert normalize_record(_raw(value=0)).value == 0
    assert normalize_record(_raw(value=200_000)).value == 200_000


@pytest.mark.parametrize("metric", [None, "", "blood_pressure", 5])
def test_unknown_metrics_are_rejected(metric):
    with pytest.raises(RecordError):
        normalize_record(_raw(metric=metric))


def test_unknown_fields_are_rejected_so_a_drifting_adapter_fails_loudly():
    _rejects("unexpected field.*importer", importer="csv")
    _rejects("unexpected field.*sourceName", sourceName="Watch")


def test_empty_optional_text_is_treated_as_missing():
    record = normalize_record(_raw(source="", source_type="  ", source_record_id="", timezone=""))
    assert (record.source, record.source_type, record.source_record_id, record.timezone) == (None, None, None, None)


@pytest.mark.parametrize("field", ["source", "source_type", "source_record_id", "timezone"])
def test_optional_text_must_be_text_and_not_huge(field):
    _rejects("must be text", **{field: 5})
    _rejects("longer than", **{field: "x" * 257})


@pytest.mark.parametrize(
    "timezone", ["+01:00", "-08:00", "+00:00", "+14:00", "UTC", "Europe/Berlin", "America/Argentina/Buenos_Aires"]
)
def test_valid_timezones_are_accepted(timezone):
    assert normalize_record(_raw(timezone=timezone)).timezone == timezone


@pytest.mark.parametrize("timezone", ["+1:00", "+15:00", "+01:60", "GMT+1", "Berlin", "Europe/", "01:00", "local"])
def test_invalid_timezones_are_rejected(timezone):
    _rejects("timezone", timezone=timezone)


@pytest.mark.parametrize("quality", [0, 0.0, 0.5, 1, 1.0])
def test_quality_inside_zero_to_one_is_accepted(quality):
    assert normalize_record(_raw(quality=quality)).quality == float(quality)


@pytest.mark.parametrize("quality", [-0.1, 1.1, "high", True, math.nan, math.inf])
def test_quality_outside_zero_to_one_or_not_a_number_is_rejected(quality):
    with pytest.raises(RecordError):
        normalize_record(_raw(quality=quality))


def test_a_batch_loads_the_valid_records_and_reports_each_rejected_one():
    batch = normalize_batch(
        [
            _raw(value=1),
            _raw(value=-5),
            _raw(timestamp=None),
            _raw(value=2, unit="lb"),
            _raw(value=3),
        ]
    )
    assert [r.value for r in batch.records] == [1, 3]
    assert [index for index, _reason in batch.rejected] == [1, 2, 3]
    assert all(isinstance(reason, str) and reason for _index, reason in batch.rejected)


def test_an_empty_batch_is_valid():
    batch = normalize_batch([])
    assert batch.records == [] and batch.rejected == []


def test_duplicate_ids_are_dropped_keeping_the_first_and_the_order():
    records = [
        normalize_record(_raw(value=1, source_record_id="a")),
        normalize_record(_raw(value=2, source_record_id="b")),
        normalize_record(_raw(value=3, source_record_id="a")),
        normalize_record(_raw(value=4)),
        normalize_record(_raw(value=4)),
    ]
    kept, dropped = drop_duplicate_ids(records)
    assert [r.value for r in kept] == [1, 2, 4, 4]
    assert dropped == 1


def test_contract_rows_load_through_the_existing_import_path(tmp_path):
    conn = connect_writable(tmp_path / "health.db")
    ensure_schema(conn)
    batch = normalize_batch(
        [
            _raw(metric="resting_heart_rate", value=58, unit="count/min", source="Watch", timezone="+01:00"),
            _raw(metric="steps", value=9000, source_record_id="r1", quality=1),
        ]
    )
    stats = import_measurements(conn, "demo", [r.to_row() for r in batch.records])
    assert (stats["added"], stats["verify"]["status"]) == (2, "ok")
    stored = conn.execute("SELECT metric, value, unit, source, importer FROM measurements ORDER BY metric").fetchall()
    assert stored == [("resting_heart_rate", 58, "bpm", "Watch", "demo"), ("steps", 9000, "count", "demo", "demo")]
