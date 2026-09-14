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

Currently supported:

- "apple-health" — a deliberately partial reading of an Apple Health
  "export.xml" (Health app -> profile icon -> Export All Health Data).
  See APPLE_HEALTH_QUANTITY_IDENTIFIERS below for exactly which record
  types are mapped; anything else in the export (blood pressure, ECG,
  mindful minutes, dozens of others) is silently ignored. There's no
  HealthKit identifier for mood, so that column is never populated by
  this adapter — log it separately with log_daily_metric.
- "health-connect" — reads Health Connect's public record JSON shape
  (a JSON array of records, or {"records": [...]}), each with a
  "recordType" key and that record type's own documented fields, as
  produced by export/interop tools built on the Health Connect API.
  This is NOT the same as Health Connect's own "Backup and restore"
  export, which is an undocumented raw SQLite snapshot and isn't
  parsed here. See adapt_health_connect for exactly which record types
  are mapped.
"""

from __future__ import annotations

import json
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
    # Individual, unaggregated observations this adapter could recover
    # provenance for — each: {"timestamp", "metric", "value", "unit",
    # "source"}. Provenance-aware sources (currently just Apple Health,
    # via each Record's `sourceName` attribute — e.g. "Ben's Apple
    # Watch") populate this; init_db.py writes it straight to the
    # measurements table (see logic.insert_measurement) alongside the
    # aggregated `rows` upsert into daily_metrics, so "which device said
    # this" survives the import instead of being lost in the day-level
    # average. Empty for adapters that can't identify a per-record
    # source (or, for the same reason, the CSV path, which init_db.py
    # handles separately from this NamedTuple entirely).
    raw_measurements: list[dict[str, Any]] = []
    # Record/recordType strings this adapter saw but doesn't map to any
    # column (e.g. Apple Health's BloodPressure, ECG, MindfulSession —
    # dozens of types this project doesn't track), each with how many
    # times it appeared. Powers `--report`'s "Unsupported" section; empty
    # for the csv path, which has no such concept.
    unsupported_types: dict[str, int] = {}


# HealthKit quantity-type identifier -> our column name, for the record
# types adapt_apple_health aggregates. Anything not listed here is ignored.
APPLE_HEALTH_QUANTITY_IDENTIFIERS: dict[str, str] = {
    "HKQuantityTypeIdentifierStepCount": "steps",
    "HKQuantityTypeIdentifierRestingHeartRate": "resting_heart_rate",
    "HKQuantityTypeIdentifierHeartRate": "heart_rate",
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": "hrv_ms",
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
    - resting_heart_rate, heart_rate: each averaged per day, rounded to
      the nearest bpm (heart_rate averages every non-resting reading in
      the day, typically many per hour from a watch)
    - hrv_ms: averaged per day (Apple records SDNN in ms already, so no
      unit conversion is needed)
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
    heart_rate_readings: dict[str, list[float]] = defaultdict(list)
    hrv_readings: dict[str, list[float]] = defaultdict(list)
    weight_latest: dict[str, tuple[datetime, float]] = {}
    sleep_seconds: dict[str, float] = defaultdict(float)
    raw_measurements: list[dict[str, Any]] = []
    unsupported_types: defaultdict[str, int] = defaultdict(int)
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
                source_name = elem.get("sourceName")
                if col == "steps":
                    step_sum[day] += value
                elif col == "resting_heart_rate":
                    resting_hr_readings[day].append(value)
                elif col == "heart_rate":
                    heart_rate_readings[day].append(value)
                elif col == "hrv_ms":
                    hrv_readings[day].append(value)
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
                # Recorded pre-conversion, in the source's own unit — this
                # is the provenance layer, not the daily_metrics aggregate
                # above, so it keeps exactly what the device reported.
                raw_measurements.append(
                    {
                        "timestamp": when.isoformat(),
                        "metric": col,
                        "value": float(elem.get("value")),
                        "unit": elem.get("unit"),
                        "source": source_name,
                    }
                )
            elif rtype == "HKCategoryTypeIdentifierSleepAnalysis" and elem.get("value") in _SLEEP_ASLEEP_VALUES:
                start_raw, end_raw = elem.get("startDate"), elem.get("endDate")
                if start_raw is None or end_raw is None:
                    raise RowError("sleep record missing startDate or endDate")
                start_dt = _parse_apple_datetime(start_raw)
                end_dt = _parse_apple_datetime(end_raw)
                sleep_seconds[start_dt.date().isoformat()] += (end_dt - start_dt).total_seconds()
            elif rtype and rtype != "HKCategoryTypeIdentifierSleepAnalysis":
                # A record type (or a Sleep record whose value isn't one
                # of _SLEEP_ASLEEP_VALUES, e.g. InBed/Awake) this adapter
                # doesn't map to a column. Counted, not treated as an
                # error — this is expected for most of what's in a real
                # export.xml (BloodPressure, ECG, MindfulSession, etc.).
                unsupported_types[rtype] += 1
        except RowError as exc:
            print(f"Skipping a record in {path}: {exc}", file=sys.stderr)
            skipped += 1
        finally:
            elem.clear()

    all_days = (
        set(step_sum)
        | set(resting_hr_readings)
        | set(heart_rate_readings)
        | set(hrv_readings)
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
            ("heart_rate", bool(heart_rate_readings)),
            ("hrv_ms", bool(hrv_readings)),
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
        if day in heart_rate_readings:
            readings = heart_rate_readings[day]
            row["heart_rate"] = int(round(sum(readings) / len(readings)))
        if day in hrv_readings:
            row["hrv_ms"] = round(sum(hrv_readings[day]) / len(hrv_readings[day]), 1)
        if day in weight_latest:
            row["weight_kg"] = round(weight_latest[day][1], 2)
        if day in exercise_sum:
            row["workout_minutes"] = int(round(exercise_sum[day]))
        if day in water_sum:
            row["water_ml"] = int(round(water_sum[day]))
        rows.append(row)

    return AdaptedImport(
        rows=rows,
        present_columns=present_columns,
        skipped=skipped,
        raw_measurements=raw_measurements,
        unsupported_types=dict(unsupported_types),
    )


# Health Connect SleepSessionRecord stage-type strings counted as
# actually asleep (as opposed to AWAKE/OUT_OF_BED/UNKNOWN), per the
# Health Connect API's SleepSessionRecord.Stage.
_HC_ASLEEP_STAGES = {
    "STAGE_TYPE_SLEEPING",
    "STAGE_TYPE_LIGHT",
    "STAGE_TYPE_DEEP",
    "STAGE_TYPE_REM",
}


def _parse_hc_datetime(raw: str) -> datetime:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise RowError(f"unparseable Health Connect timestamp {raw!r}") from exc


def adapt_health_connect(path: Path) -> AdaptedImport:
    """Parse a Health Connect record-JSON export (see the module
    docstring for exactly which export shape this expects) and aggregate
    per calendar day:

    - steps: summed StepsRecord.count, attributed to startTime's day
    - heart_rate: averaged HeartRateRecord sample beatsPerMinute
    - resting_heart_rate: averaged RestingHeartRateRecord.beatsPerMinute
    - hrv_ms: averaged HeartRateVariabilityRmssdRecord.heartRateVariabilityMillis
    - weight_kg: the day's latest WeightRecord.weight.value (converted
      from pounds if that record's unit says so)
    - workout_minutes: summed ExerciseSessionRecord duration
    - sleep_hours: summed duration of SleepSessionRecord stages in
      _HC_ASLEEP_STAGES, or the whole session if a record has no stages
      (attributed to the day the session/stage started)

    Any other recordType is silently ignored, same as an unmapped Apple
    Health identifier.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        sys.exit(f"Error: couldn't read {path} as Health Connect JSON: {exc}")
    records = data.get("records", data) if isinstance(data, dict) else data
    if not isinstance(records, list):
        sys.exit(f"Error: {path} doesn't look like a Health Connect export (expected a JSON array of records).")

    step_sum: dict[str, float] = defaultdict(float)
    heart_rate_readings: dict[str, list[float]] = defaultdict(list)
    resting_hr_readings: dict[str, list[float]] = defaultdict(list)
    hrv_readings: dict[str, list[float]] = defaultdict(list)
    weight_latest: dict[str, tuple[datetime, float]] = {}
    exercise_minutes: dict[str, float] = defaultdict(float)
    sleep_seconds: dict[str, float] = defaultdict(float)
    unsupported_types: defaultdict[str, int] = defaultdict(int)
    skipped = 0

    for record in records:
        try:
            rtype = record.get("recordType")
            if rtype == "StepsRecord":
                when = _parse_hc_datetime(record["startTime"])
                step_sum[when.date().isoformat()] += float(record["count"])
            elif rtype == "HeartRateRecord":
                for sample in record.get("samples", []):
                    when = _parse_hc_datetime(sample["time"])
                    heart_rate_readings[when.date().isoformat()].append(float(sample["beatsPerMinute"]))
            elif rtype == "RestingHeartRateRecord":
                when = _parse_hc_datetime(record["time"])
                resting_hr_readings[when.date().isoformat()].append(float(record["beatsPerMinute"]))
            elif rtype in ("HeartRateVariabilityRmssdRecord", "HeartRateVariabilityRecord"):
                when = _parse_hc_datetime(record["time"])
                ms = record.get("heartRateVariabilityMillis", record.get("heartRateVariabilityRmssd"))
                hrv_readings[when.date().isoformat()].append(float(ms))
            elif rtype == "WeightRecord":
                when = _parse_hc_datetime(record["time"])
                weight = record["weight"]
                value, unit = float(weight["value"]), (weight.get("unit") or "").strip().lower()
                if unit in _LB_UNITS:
                    value *= 0.45359237
                day = when.date().isoformat()
                prior = weight_latest.get(day)
                if prior is None or when > prior[0]:
                    weight_latest[day] = (when, value)
            elif rtype == "ExerciseSessionRecord":
                start = _parse_hc_datetime(record["startTime"])
                end = _parse_hc_datetime(record["endTime"])
                exercise_minutes[start.date().isoformat()] += (end - start).total_seconds() / 60
            elif rtype == "SleepSessionRecord":
                start = _parse_hc_datetime(record["startTime"])
                day = start.date().isoformat()
                stages = record.get("stages") or []
                if stages:
                    for stage in stages:
                        if stage.get("stage") in _HC_ASLEEP_STAGES:
                            s = _parse_hc_datetime(stage["startTime"])
                            e = _parse_hc_datetime(stage["endTime"])
                            sleep_seconds[day] += (e - s).total_seconds()
                else:
                    end = _parse_hc_datetime(record["endTime"])
                    sleep_seconds[day] += (end - start).total_seconds()
            elif rtype:
                # A recordType this adapter doesn't map to a column (e.g.
                # BloodPressureRecord, OxygenSaturationRecord, dozens of
                # others Health Connect exposes). Counted, not an error.
                unsupported_types[rtype] += 1
        except (KeyError, TypeError, ValueError, RowError) as exc:
            print(f"Skipping a record in {path}: {exc}", file=sys.stderr)
            skipped += 1

    all_days = (
        set(step_sum)
        | set(heart_rate_readings)
        | set(resting_hr_readings)
        | set(hrv_readings)
        | set(weight_latest)
        | set(exercise_minutes)
        | set(sleep_seconds)
    )

    present_columns = [
        col
        for col, has_data in (
            ("steps", bool(step_sum)),
            ("sleep_hours", bool(sleep_seconds)),
            ("heart_rate", bool(heart_rate_readings)),
            ("resting_heart_rate", bool(resting_hr_readings)),
            ("hrv_ms", bool(hrv_readings)),
            ("weight_kg", bool(weight_latest)),
            ("workout_minutes", bool(exercise_minutes)),
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
        if day in heart_rate_readings:
            readings = heart_rate_readings[day]
            row["heart_rate"] = int(round(sum(readings) / len(readings)))
        if day in resting_hr_readings:
            readings = resting_hr_readings[day]
            row["resting_heart_rate"] = int(round(sum(readings) / len(readings)))
        if day in hrv_readings:
            row["hrv_ms"] = round(sum(hrv_readings[day]) / len(hrv_readings[day]), 1)
        if day in weight_latest:
            row["weight_kg"] = round(weight_latest[day][1], 2)
        if day in exercise_minutes:
            row["workout_minutes"] = int(round(exercise_minutes[day]))
        rows.append(row)

    return AdaptedImport(
        rows=rows,
        present_columns=present_columns,
        skipped=skipped,
        unsupported_types=dict(unsupported_types),
    )


def detect_adapter(path: Path) -> str:
    """Best-effort guess at which adapter a source file needs, by file
    extension. Only used for --source auto (the CLI default) — an
    explicit --source always wins over this.
    """
    return "apple-health" if path.suffix.lower() == ".xml" else "csv"


ADAPTERS: dict[str, Callable[[Path], AdaptedImport]] = {
    "apple-health": adapt_apple_health,
    "health-connect": adapt_health_connect,
}
