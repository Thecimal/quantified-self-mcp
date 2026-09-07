"""
Unit tests for import_adapters.py.

Run with: pytest

These build small synthetic Apple Health export.xml fixtures rather than
shipping a real export (which would be huge and contain someone's actual
health data) — just enough of the real format's structure to exercise
each aggregation rule in adapt_apple_health.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from import_adapters import ADAPTERS, adapt_apple_health, detect_adapter  # noqa: E402


def _write_export(tmp_path, records_xml: str) -> Path:
    path = tmp_path / "export.xml"
    path.write_text(
        f'<?xml version="1.0" encoding="UTF-8"?>\n<HealthData locale="en_US">\n{records_xml}\n</HealthData>\n',
        encoding="utf-8",
    )
    return path


def test_detect_adapter_picks_apple_health_for_xml_and_csv_otherwise(tmp_path):
    assert detect_adapter(Path("export.xml")) == "apple-health"
    assert detect_adapter(Path("EXPORT.XML")) == "apple-health"
    assert detect_adapter(Path("health.csv")) == "csv"
    assert detect_adapter(Path("health.txt")) == "csv"


def test_apple_health_sums_steps_across_multiple_records_in_a_day(tmp_path):
    path = _write_export(
        tmp_path,
        """
        <Record type="HKQuantityTypeIdentifierStepCount" unit="count"
                startDate="2026-01-15 08:00:00 -0500" endDate="2026-01-15 08:05:00 -0500" value="120"/>
        <Record type="HKQuantityTypeIdentifierStepCount" unit="count"
                startDate="2026-01-15 09:00:00 -0500" endDate="2026-01-15 09:05:00 -0500" value="380"/>
        """,
    )
    result = adapt_apple_health(path)
    assert result.rows == [{"date": "2026-01-15", "steps": 500}]
    assert result.present_columns == ["steps"]
    assert result.skipped == 0


def test_apple_health_averages_resting_heart_rate(tmp_path):
    path = _write_export(
        tmp_path,
        """
        <Record type="HKQuantityTypeIdentifierRestingHeartRate" unit="count/min"
                startDate="2026-01-15 06:00:00 -0500" endDate="2026-01-15 06:00:00 -0500" value="58"/>
        <Record type="HKQuantityTypeIdentifierRestingHeartRate" unit="count/min"
                startDate="2026-01-15 22:00:00 -0500" endDate="2026-01-15 22:00:00 -0500" value="62"/>
        """,
    )
    result = adapt_apple_health(path)
    assert result.rows == [{"date": "2026-01-15", "resting_heart_rate": 60}]


def test_apple_health_converts_body_mass_from_pounds(tmp_path):
    path = _write_export(
        tmp_path,
        """
        <Record type="HKQuantityTypeIdentifierBodyMass" unit="lb"
                startDate="2026-01-15 07:00:00 -0500" endDate="2026-01-15 07:00:00 -0500" value="154.32"/>
        """,
    )
    result = adapt_apple_health(path)
    assert result.rows == [{"date": "2026-01-15", "weight_kg": 70.0}]


def test_apple_health_body_mass_keeps_only_the_latest_reading_per_day(tmp_path):
    path = _write_export(
        tmp_path,
        """
        <Record type="HKQuantityTypeIdentifierBodyMass" unit="kg"
                startDate="2026-01-15 07:00:00 -0500" endDate="2026-01-15 07:00:00 -0500" value="70.5"/>
        <Record type="HKQuantityTypeIdentifierBodyMass" unit="kg"
                startDate="2026-01-15 19:00:00 -0500" endDate="2026-01-15 19:00:00 -0500" value="70.1"/>
        """,
    )
    result = adapt_apple_health(path)
    assert result.rows == [{"date": "2026-01-15", "weight_kg": 70.1}]


def test_apple_health_sums_exercise_time_and_dietary_water_with_unit_conversion(tmp_path):
    path = _write_export(
        tmp_path,
        """
        <Record type="HKQuantityTypeIdentifierAppleExerciseTime" unit="min"
                startDate="2026-01-15 18:00:00 -0500" endDate="2026-01-15 18:30:00 -0500" value="30"/>
        <Record type="HKQuantityTypeIdentifierDietaryWater" unit="L"
                startDate="2026-01-15 12:00:00 -0500" endDate="2026-01-15 12:00:00 -0500" value="0.5"/>
        <Record type="HKQuantityTypeIdentifierDietaryWater" unit="mL"
                startDate="2026-01-15 16:00:00 -0500" endDate="2026-01-15 16:00:00 -0500" value="250"/>
        """,
    )
    result = adapt_apple_health(path)
    row = result.rows[0]
    assert row["workout_minutes"] == 30
    assert row["water_ml"] == 750


def test_apple_health_sleep_analysis_attributed_to_the_night_it_started(tmp_path):
    """A sleep interval that starts the evening of the 14th and ends the
    morning of the 15th should count entirely toward the 14th (the night
    that sleep belongs to), not get split across two days.
    """
    path = _write_export(
        tmp_path,
        """
        <Record type="HKCategoryTypeIdentifierSleepAnalysis" value="HKCategoryValueSleepAnalysisAsleepCore"
                startDate="2026-01-14 23:00:00 -0500" endDate="2026-01-15 06:30:00 -0500"/>
        """,
    )
    result = adapt_apple_health(path)
    assert result.rows == [{"date": "2026-01-14", "sleep_hours": 7.5}]


def test_apple_health_ignores_sleep_in_bed_but_not_asleep(tmp_path):
    path = _write_export(
        tmp_path,
        """
        <Record type="HKCategoryTypeIdentifierSleepAnalysis" value="HKCategoryValueSleepAnalysisInBed"
                startDate="2026-01-14 22:30:00 -0500" endDate="2026-01-15 06:30:00 -0500"/>
        """,
    )
    result = adapt_apple_health(path)
    assert result.rows == []
    assert result.present_columns == []


def test_apple_health_ignores_unmapped_record_types(tmp_path):
    path = _write_export(
        tmp_path,
        """
        <Record type="HKQuantityTypeIdentifierBloodPressureSystolic" unit="mmHg"
                startDate="2026-01-15 07:00:00 -0500" endDate="2026-01-15 07:00:00 -0500" value="118"/>
        """,
    )
    result = adapt_apple_health(path)
    assert result.rows == []
    assert result.present_columns == []
    assert result.skipped == 0  # not an error, just nothing we map


def test_apple_health_skips_records_with_unparseable_data_but_keeps_the_rest(tmp_path):
    path = _write_export(
        tmp_path,
        """
        <Record type="HKQuantityTypeIdentifierStepCount" unit="count"
                startDate="not-a-date" endDate="2026-01-15 08:05:00 -0500" value="120"/>
        <Record type="HKQuantityTypeIdentifierStepCount" unit="count"
                startDate="2026-01-15 09:00:00 -0500" endDate="2026-01-15 09:05:00 -0500" value="not-a-number"/>
        <Record type="HKQuantityTypeIdentifierStepCount" unit="count"
                startDate="2026-01-15 10:00:00 -0500" endDate="2026-01-15 10:05:00 -0500" value="200"/>
        """,
    )
    result = adapt_apple_health(path)
    assert result.rows == [{"date": "2026-01-15", "steps": 200}]
    assert result.skipped == 2


def test_apple_health_multiple_days_produce_separate_rows(tmp_path):
    path = _write_export(
        tmp_path,
        """
        <Record type="HKQuantityTypeIdentifierStepCount" unit="count"
                startDate="2026-01-15 08:00:00 -0500" endDate="2026-01-15 08:05:00 -0500" value="1000"/>
        <Record type="HKQuantityTypeIdentifierStepCount" unit="count"
                startDate="2026-01-16 08:00:00 -0500" endDate="2026-01-16 08:05:00 -0500" value="2000"/>
        """,
    )
    result = adapt_apple_health(path)
    assert result.rows == [
        {"date": "2026-01-15", "steps": 1000},
        {"date": "2026-01-16", "steps": 2000},
    ]


def test_apple_health_is_registered_in_adapters():
    assert ADAPTERS["apple-health"] is adapt_apple_health
