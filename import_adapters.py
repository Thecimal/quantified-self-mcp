"""
import_adapters.py
===================
Adapters that turn a health-data export from some external source into
the canonical per-day row shape init_db.py upserts: a "date" key
(YYYY-MM-DD) plus whichever metric columns that source provides, already
parsed to the right Python type and ready for logic.validate_metrics.

init_db.py's own CSV format is read directly in init_db.py (see
_read_csv there) since it long predates this module and its existing
tests parse raw string values from CSV specifically — this module is for
*additional* adapters, registered in ADAPTERS below. Add support for a new
export format by writing one function here with the
`Callable[[Path], AdaptedImport]` signature and adding it to ADAPTERS;
init_db.py's CLI and its validate_metrics/upsert_metrics pipeline don't
need to change.

Currently supported: "apple-health" — a deliberately partial reading of
an Apple Health "export.xml" (Health app -> profile icon -> Export All
Health Data). See APPLE_HEALTH_QUANTITY_IDENTIFIERS below for exactly
which record types are mapped; anything else in the export (blood
pressure, ECG, mindful minutes, dozens of others) is silently ignored.
There's no HealthKit identifier for mood, so that column is never
populated by this adapter — log it separately with log_daily_metric.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple


class RowError(ValueError):
    """A single source record couldn't be parsed; the import continues
    without it. Shared with init_db.py's CSV reader so every adapter's
    row-level failures are caught and reported the same way, and so
    `except RowError` in one place catches failures from any adapter.
    """


class AdaptedImport(NamedTuple):
    rows: list[dict[str, Any]]  # each: {"date": "YYYY-MM-DD", <metric>: <typed value>, ...}
    present_columns: list[str]  # canonical metric column names that appeared anywhere in the source
    skipped: int  # source records this adapter itself couldn't parse (already printed to stderr)


# HealthKit quantity-type identifier -> our column name, for the record
# types adapt_apple_health aggregates. Anything not listed here is ignored.
APPLE_HEALTH_QUANTITY_IDENTIFIERS: dict[str, str] = {
    "HKQuantityTypeIdentifierStepCount": "steps",
    "HKQuantityTypeIdentifierRestingHeartRate": "resting_heart_rate",
    "HKQuantityTypeIdentifierBodyMass": "weight_kg",
    "HKQuantityTypeIdentifierAppleExerciseTime": "workout_minutes",
    "HKQuantityTypeIdentifierDietaryWater": "water_ml",
}

# HKCategoryTypeIdentifierSleepAnalysis values counted as time actually
# asleep (as opposed to e.g. HKCategoryValueSleepAnalysisInBed, which
# covers time in bed but not necessarily asleep, or ...Awake).
_SLEEP_ASLEEP_VALUES = {
    "HKCategoryValueSleepAnalysisAsleep",
    "HKCategoryValueSleepAnalysisAsleepCore",
    "HKCategoryValueSleepAnalysisAsleepDeep",
    "HKCategoryValueSleepAnalysisAsleepREM",
    "HKCategoryValueSleepAnalysisAsleepUnspecified",
}

# Apple Health's own datetime format, e.g. "2026-01-15 08:30:00 -0500".
_APPLE_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S %z"

_LB_UNITS = {"lb", "lbs", "pound", "pounds"}
_LITER_UNITS = {"l", "liter", "liters", "litre", "litres"}


def _parse_apple_datetime(raw: str) -> datetime:
    try:
        return datetime.strptime(raw.strip(), _APPLE_DATETIME_FORMAT)
    except ValueError as exc:
        raise RowError(f"unparseable Apple Health datetime {raw!r}") from exc


def adapt_apple_health(path: Path) -> AdaptedImport:
    """Stream-parse an Apple Health export.xml (potentially hundreds of MB
    — hence iterparse rather than loading a full tree) and aggregate the
    mapped record types per calendar day:

    - steps, workout_minutes, water_ml: summed per day (workout_minutes
      sums HKQuantityTypeIdentifierAppleExerciseTime rather than Workout
      elements, since the latter's many activity types would each need
      their own separate mapping decision this adapter doesn't make)
    - resting_heart_rate: averaged per day, rounded to the nearest bpm
    - weight_kg: the day's latest reading by startDate (converted from lb
      if that record's unit attribute says so)
    - sleep_hours: summed duration (endDate - startDate) of "asleep"
      HKCategoryTypeIdentifierSleepAnalysis intervals, attributed to the
      calendar day the interval started on — a night that crosses
      midnight is counted entirely on the day it started, not split
    """
    step_sum: dict[str, float] = defaultdict(float)
    exercise_sum: dict[str, float] = defaultdict(float)
    water_sum: dict[str, float] = defaultdict(float)
    resting_hr_readings: dict[str, list[float]] = defaultdict(list)
    weight_latest: dict[str, tuple[datetime, float]] = {}
    sleep_seconds: dict[str, float] = defaultdict(float)
    skipped = 0

    for _, elem in ET.iterparse(str(path), events=("end",)):
        if elem.tag != "Record":
            continue
        try:
            rtype = elem.get("type")
            if rtype in APPLE_HEALTH_QUANTITY_IDENTIFIERS:
                start_raw = elem.get("startDate")
                value_raw = elem.get("value")
                if start_raw is None or value_raw is None:
                    raise RowError(f"{rtype} record missing startDate or value")
                when = _parse_apple_datetime(start_raw)
                day = when.date().isoformat()
                try:
                    value = float(value_raw)
                except ValueError as exc:
                    raise RowError(f"{rtype} record has non-numeric value {value_raw!r}") from exc
                unit = (elem.get("unit") or "").strip().lower()
                col = APPLE_HEALTH_QUANTITY_IDENTIFIERS[rtype]
                if col == "steps":
                    step_sum[day] += value
                elif col == "resting_heart_rate":
                    resting_hr_readings[day].append(value)
                elif col == "weight_kg":
                    if unit in _LB_UNITS:
                        value *= 0.45359237
                    prior = weight_latest.get(day)
                    if prior is None or when > prior[0]:
                        weight_latest[day] = (when, value)
                elif col == "workout_minutes":
                    exercise_sum[day] += value
                elif col == "water_ml":
                    if unit in _LITER_UNITS:
                        value *= 1000
                    water_sum[day] += value
            elif rtype == "HKCategoryTypeIdentifierSleepAnalysis" and elem.get("value") in _SLEEP_ASLEEP_VALUES:
                start_raw, end_raw = elem.get("startDate"), elem.get("endDate")
                if start_raw is None or end_raw is None:
                    raise RowError("sleep record missing startDate or endDate")
                start_dt = _parse_apple_datetime(start_raw)
                end_dt = _parse_apple_datetime(end_raw)
                sleep_seconds[start_dt.date().isoformat()] += (end_dt - start_dt).total_seconds()
        except RowError as exc:
            print(f"Skipping a record in {path}: {exc}", file=sys.stderr)
            skipped += 1
        finally:
            elem.clear()

    all_days = (
        set(step_sum)
        | set(resting_hr_readings)
        | set(weight_latest)
        | set(exercise_sum)
        | set(water_sum)
        | set(sleep_seconds)
    )

    present_columns = [
        col
        for col, has_data in (
            ("steps", bool(step_sum)),
            ("sleep_hours", bool(sleep_seconds)),
            ("resting_heart_rate", bool(resting_hr_readings)),
            ("weight_kg", bool(weight_latest)),
            ("workout_minutes", bool(exercise_sum)),
            ("water_ml", bool(water_sum)),
        )
        if has_data
    ]

    rows: list[dict[str, Any]] = []
    for day in sorted(all_days):
        row: dict[str, Any] = {"date": day}
        if day in step_sum:
            row["steps"] = int(round(step_sum[day]))
        if day in sleep_seconds:
            row["sleep_hours"] = round(sleep_seconds[day] / 3600, 2)
        if day in resting_hr_readings:
            readings = resting_hr_readings[day]
            row["resting_heart_rate"] = int(round(sum(readings) / len(readings)))
        if day in weight_latest:
            row["weight_kg"] = round(weight_latest[day][1], 2)
        if day in exercise_sum:
            row["workout_minutes"] = int(round(exercise_sum[day]))
        if day in water_sum:
            row["water_ml"] = int(round(water_sum[day]))
        rows.append(row)

    return AdaptedImport(rows=rows, present_columns=present_columns, skipped=skipped)


def detect_adapter(path: Path) -> str:
    """Best-effort guess at which adapter a source file needs, by file
    extension. Only used for --source auto (the CLI default) — an
    explicit --source always wins over this.
    """
    return "apple-health" if path.suffix.lower() == ".xml" else "csv"


ADAPTERS: dict[str, Callable[[Path], AdaptedImport]] = {
    "apple-health": adapt_apple_health,
}
