"""
Regression tests: imported observations must reach daily_metrics in the
metric's canonical unit, within bounds, and never as NaN/inf.

daily_metrics is a projection of `measurements` (db/aggregation.py), so what an
importer writes as a raw observation is exactly what analytics later sees.
Each test drives init_health_db end to end and then inspects both tables.
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import invariant  # noqa: E402
from import_adapters import (  # noqa: E402
    RowError,  # noqa: E402
    adapt_apple_health,
    adapt_health_connect,
)
from init_db import _to_float, _to_int, init_health_db  # noqa: E402


def _apple(tmp_path, *records: str) -> Path:
    path = tmp_path / "export.xml"
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<HealthData locale="en_US">\n'
        + "\n".join(records)
        + "\n</HealthData>\n",
        encoding="utf-8",
    )
    return path


def _rec(kind: str, value: str, unit: str, when: str, source: str = "Dev") -> str:
    return (
        f'<Record type="HKQuantityTypeIdentifier{kind}" sourceName="{source}" unit="{unit}" '
        f'startDate="{when} +0000" endDate="{when} +0000" value="{value}"/>'
    )


def _load(path: Path, tmp_path, **kw):
    db = tmp_path / "health.db"
    init_health_db(path, db, replace=kw.pop("replace", False), **kw)
    return db


def _rows(db: Path, sql: str):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _assert_projection_clean(db: Path):
    conn = sqlite3.connect(db)
    try:
        assert invariant.verify(conn)["status"] == "ok"
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Units: raw observations are stored canonical, so the projection is right
# --------------------------------------------------------------------------


def test_pound_body_mass_is_projected_as_kilograms(tmp_path):
    db = _load(_apple(tmp_path, _rec("BodyMass", "154", "lb", "2026-01-06 08:00:00")), tmp_path)
    ((metric, value, unit),) = _rows(db, "SELECT metric, value, unit FROM measurements")
    assert (metric, unit) == ("weight_kg", "kg")
    assert value == pytest.approx(154 * 0.45359237)
    ((_, daily),) = _rows(db, "SELECT date, value FROM daily_metrics WHERE metric = 'weight_kg'")
    assert daily == pytest.approx(69.85, abs=0.01)
    _assert_projection_clean(db)


def test_litre_water_is_summed_as_millilitres(tmp_path):
    db = _load(
        _apple(
            tmp_path,
            _rec("DietaryWater", "2", "L", "2026-01-06 09:00:00"),
            _rec("DietaryWater", "250", "mL", "2026-01-06 12:00:00"),
        ),
        tmp_path,
    )
    assert _rows(db, "SELECT value, unit FROM measurements ORDER BY timestamp") == [(2000.0, "ml"), (250.0, "mL")]
    assert _rows(db, "SELECT value FROM daily_metrics WHERE metric = 'water_ml'") == [(2250.0,)]
    _assert_projection_clean(db)


@pytest.mark.parametrize(
    ("kind", "unit", "value"),
    [("BodyMass", "st", "11"), ("DietaryWater", "fl_oz_us", "16")],
)
def test_unrecognised_unit_is_skipped_not_read_as_canonical(tmp_path, kind, unit, value):
    adapted = adapt_apple_health(_apple(tmp_path, _rec(kind, value, unit, "2026-01-06 08:00:00")))
    assert adapted.raw_measurements == []
    assert adapted.rows == []
    assert adapted.skipped == 1


# --------------------------------------------------------------------------
# Invalid observations reach neither table
# --------------------------------------------------------------------------


def test_out_of_bounds_observation_is_not_stored_or_projected(tmp_path):
    db = _load(
        _apple(
            tmp_path,
            _rec("StepCount", "-500", "count", "2026-01-08 08:00:00"),
            _rec("HeartRate", "-40", "count/min", "2026-01-07 08:00:00"),
            _rec("StepCount", "1000", "count", "2026-01-09 08:00:00"),
        ),
        tmp_path,
    )
    assert _rows(db, "SELECT metric, value FROM measurements") == [("steps", 1000.0)]
    assert _rows(db, "SELECT date, metric, value FROM daily_metrics") == [("2026-01-09", "steps", 1000.0)]
    _assert_projection_clean(db)


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf"])
def test_non_finite_apple_record_is_skipped_and_import_completes(tmp_path, bad):
    db = _load(
        _apple(
            tmp_path,
            _rec("StepCount", bad, "count", "2026-01-05 08:00:00"),
            _rec("HeartRate", bad, "count/min", "2026-01-05 09:00:00"),
            _rec("StepCount", "1000", "count", "2026-01-06 08:00:00"),
        ),
        tmp_path,
    )
    assert _rows(db, "SELECT date, metric, value FROM daily_metrics") == [("2026-01-06", "steps", 1000.0)]
    _assert_projection_clean(db)


def test_day_skipped_for_an_out_of_bounds_total_imports_no_raw_rows(tmp_path):
    # Each reading is valid; the day's sum (250000) exceeds the daily bound, so
    # the day is reported as skipped and none of its observations may land.
    db = _load(
        _apple(
            tmp_path,
            _rec("StepCount", "150000", "count", "2026-01-05 08:00:00"),
            _rec("StepCount", "100000", "count", "2026-01-05 18:00:00"),
            _rec("StepCount", "900", "count", "2026-01-06 08:00:00"),
        ),
        tmp_path,
    )
    assert _rows(db, "SELECT date, value FROM daily_metrics WHERE metric = 'steps'") == [("2026-01-06", 900.0)]
    assert _rows(db, "SELECT COUNT(*) FROM measurements WHERE date(timestamp) = '2026-01-05'") == [(0,)]
    _assert_projection_clean(db)


# --------------------------------------------------------------------------
# CSV parsing
# --------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["inf", "-inf", "nan", "1e999"])
def test_csv_number_parsers_reject_non_finite_values(raw):
    with pytest.raises(RowError):
        _to_int(raw)
    with pytest.raises(RowError):
        _to_float(raw)


def test_csv_with_infinite_step_count_skips_the_row_instead_of_crashing(tmp_path):
    csv = tmp_path / "d.csv"
    csv.write_text("date,steps\n2026-02-01,inf\n2026-02-02,500\n", encoding="utf-8")
    db = _load(csv, tmp_path)
    assert _rows(db, "SELECT date, value FROM daily_metrics WHERE metric = 'steps'") == [("2026-02-02", 500.0)]
    _assert_projection_clean(db)


# --------------------------------------------------------------------------
# Health Connect
# --------------------------------------------------------------------------


def _hc(tmp_path, records: list[dict]) -> Path:
    path = tmp_path / "hc.json"
    path.write_text(json.dumps({"records": records}), encoding="utf-8")
    return path


def test_health_connect_non_finite_value_is_skipped_not_aggregated(tmp_path):
    def steps(day: str, count) -> dict:
        return {
            "recordType": "StepsRecord",
            "startTime": f"{day}T08:00:00Z",
            "endTime": f"{day}T09:00:00Z",
            "count": count,
        }

    records = [steps("2026-01-05", "NaN"), steps("2026-01-06", 700)]
    adapted = adapt_health_connect(_hc(tmp_path, records))
    assert [r["date"] for r in adapted.rows] == ["2026-01-06"]
    assert adapted.skipped == 1


def test_health_connect_unrecognised_weight_unit_is_skipped(tmp_path):
    records = [{"recordType": "WeightRecord", "time": "2026-01-05T08:00:00Z", "weight": {"value": 11, "unit": "st"}}]
    adapted = adapt_health_connect(_hc(tmp_path, records))
    assert adapted.rows == []
    assert adapted.skipped == 1


# --------------------------------------------------------------------------
# Repeat imports
# --------------------------------------------------------------------------


@pytest.mark.parametrize("replace", [False, True])
def test_reimporting_converted_units_does_not_change_the_projection(tmp_path, replace):
    path = _apple(
        tmp_path,
        _rec("BodyMass", "154", "lb", "2026-01-06 08:00:00"),
        _rec("DietaryWater", "2", "L", "2026-01-06 09:00:00"),
    )
    db = tmp_path / "health.db"
    init_health_db(path, db, replace=replace)
    first = _rows(db, "SELECT date, metric, value FROM daily_metrics ORDER BY metric")
    init_health_db(path, db, replace=replace)
    assert _rows(db, "SELECT date, metric, value FROM daily_metrics ORDER BY metric") == first
    assert _rows(db, "SELECT COUNT(*) FROM measurements") == [(2,)]
    _assert_projection_clean(db)


def test_health_connect_rejected_heart_rate_leaves_no_phantom_day(tmp_path):
    # A rejected reading must not create an empty per-day bucket (which would
    # otherwise divide by zero when the day's mean is taken).
    records = [
        {
            "recordType": "HeartRateRecord",
            "startTime": "2026-01-05T08:00:00Z",
            "endTime": "2026-01-05T08:05:00Z",
            "samples": [{"time": "2026-01-05T08:00:00Z", "beatsPerMinute": "inf"}],
        }
    ]
    adapted = adapt_health_connect(_hc(tmp_path, records))
    assert adapted.rows == []
