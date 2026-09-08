"""
generate_sample_data.py
========================
Deterministically generates the example datasets under sample_data/ —
health_sample.csv and apple_health_sample.xml — from one shared,
seeded random walk, so both files always describe the same 30 fictional
days of health data in two different formats.

"Reproducible" means exactly that: `python generate_sample_data.py` with
no arguments regenerates health_sample.csv and apple_health_sample.xml
byte-for-byte identical to what's already checked in (verified by
tests/test_sample_data.py) — nobody has to guess how these files were
made, or hand-edit them to add a new day. Pass --seed/--days/--start-date
to generate a different dataset instead (a longer one for testing, say),
or --output/--xml-output to write somewhere else without touching the
checked-in files.

Usage:
    python generate_sample_data.py
    python generate_sample_data.py --days 90 --output /tmp/longer_sample.csv
    python generate_sample_data.py --seed 7 --output /tmp/different_seed.csv
"""

from __future__ import annotations

import argparse
import random
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path
from xml.dom import minidom

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logic import METRIC_BOUNDS  # noqa: E402

DEFAULT_SEED = 42
DEFAULT_DAYS = 30
DEFAULT_START_DATE = date(2026, 7, 25)

# One day in roughly ten is missing each optional metric (steps,
# resting_heart_rate, and sleep_hours are always present, matching a
# phone/watch that tracks those automatically every day) — the sample
# data itself demonstrates the "every field is optional per day" behavior
# README.md describes, rather than just asserting it.
OPTIONAL_COLUMNS = ["weight_kg", "workout_minutes", "mood", "water_ml"]
MISSING_RATE = 0.1

# Workout minutes: mostly rest days, occasionally a session. A weighted
# choice (rather than a uniform range) reads as far more like a real
# activity log than "workout_minutes = random 0-60 every day" would.
WORKOUT_MINUTES_CHOICES = [0, 0, 0, 0, 20, 30, 45, 60]


def generate_rows(
    seed: int = DEFAULT_SEED, days: int = DEFAULT_DAYS, start_date: date = DEFAULT_START_DATE
) -> list[dict]:
    """The single source of truth both write_csv and write_apple_health_xml
    export from — one seeded random walk, in two formats, rather than two
    independently-random datasets that happen to sit in the same folder.

    Returns one dict per day: {"date": date, "steps": int,
    "sleep_hours": float, "resting_heart_rate": int, and (unless randomly
    omitted, per MISSING_RATE) "weight_kg", "workout_minutes", "mood",
    "water_ml"}. Every value is generated within METRIC_BOUNDS, so the
    output always passes logic.validate_metrics unchanged.
    """
    rng = random.Random(seed)
    rows = []
    # A slow, mildly noisy downward drift, not a straight line — more
    # like a real weight trend than either pure noise or a perfect ramp.
    weight = rng.uniform(76.0, 79.0)

    for i in range(days):
        day = start_date + timedelta(days=i)
        weight += rng.uniform(-0.15, 0.1)
        weight = min(max(weight, METRIC_BOUNDS["weight_kg"][0] + 1), METRIC_BOUNDS["weight_kg"][1] - 1)

        row: dict = {
            "date": day,
            "steps": rng.randint(4000, 13000),
            "sleep_hours": round(rng.uniform(5.5, 8.6), 1),
            "resting_heart_rate": rng.randint(54, 68),
        }
        if rng.random() > MISSING_RATE:
            row["weight_kg"] = round(weight, 1)
        if rng.random() > MISSING_RATE:
            row["workout_minutes"] = rng.choice(WORKOUT_MINUTES_CHOICES)
        if rng.random() > MISSING_RATE:
            row["mood"] = rng.randint(2, 5)
        if rng.random() > MISSING_RATE:
            row["water_ml"] = rng.randint(1200, 2800)
        rows.append(row)

    return rows


def write_csv(rows: list[dict], path: Path) -> None:
    """This project's own CSV format (see init_db.py) — a day with a
    missing optional metric just omits that column's value for that row,
    exactly as a real partial export would, and exactly what
    import_adapters.py's CSV reader is meant to handle.
    """
    columns = ["date", "steps", "sleep_hours", "resting_heart_rate", *OPTIONAL_COLUMNS]
    lines = [",".join(columns)]
    for row in rows:
        lines.append(",".join(str(row["date"]) if c == "date" else str(row.get(c, "")) for c in columns))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


# Apple Health's own datetime format (see import_adapters.py).
_APPLE_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S -0500"


def _apple_record(record_type: str, unit: str, value: object, when: datetime) -> ET.Element:
    stamp = when.strftime(_APPLE_DATETIME_FORMAT)
    return ET.Element(
        "Record",
        {
            "type": record_type,
            "sourceName": "Sample Data Generator",
            "unit": unit,
            "startDate": stamp,
            "endDate": stamp,
            "value": str(value),
        },
    )


def write_apple_health_xml(rows: list[dict], path: Path) -> None:
    """A synthetic Apple Health export.xml covering the same days and
    values as write_csv, for trying out `init_db.py --source apple-health`
    without a real export. Only maps what import_adapters.adapt_apple_health
    reads (see APPLE_HEALTH_QUANTITY_IDENTIFIERS there); mood has no
    HealthKit equivalent, so — like a real export — it's simply absent
    here rather than faked.

    Each day contributes one Record per metric it has (rather than the
    many small samples a real Watch would produce for steps, say) — this
    is meant as a small, readable adapter fixture, not a realistic export
    in scale.
    """
    root = ET.Element("HealthData", {"locale": "en_US"})
    for row in rows:
        day = row["date"]
        morning = datetime.combine(day, datetime.min.time()).replace(hour=8)
        root.append(_apple_record("HKQuantityTypeIdentifierStepCount", "count", row["steps"], morning))
        root.append(
            _apple_record(
                "HKQuantityTypeIdentifierRestingHeartRate", "count/min", row["resting_heart_rate"], morning
            )
        )
        if "weight_kg" in row:
            root.append(_apple_record("HKQuantityTypeIdentifierBodyMass", "kg", row["weight_kg"], morning))
        if row.get("workout_minutes"):  # skip 0-minute "workouts" — a real export wouldn't log a non-event
            evening = morning.replace(hour=18)
            root.append(
                _apple_record("HKQuantityTypeIdentifierAppleExerciseTime", "min", row["workout_minutes"], evening)
            )
        if "water_ml" in row:
            noon = morning.replace(hour=12)
            root.append(_apple_record("HKQuantityTypeIdentifierDietaryWater", "mL", row["water_ml"], noon))

        # A sleep interval the night before `day`, matching
        # import_adapters.py's "attributed to the night it started" rule
        # — sleeping sleep_hours before waking at 07:00 on `day`.
        wake = datetime.combine(day, datetime.min.time()).replace(hour=7)
        bedtime = wake - timedelta(hours=row["sleep_hours"])
        sleep_elem = ET.Element(
            "Record",
            {
                "type": "HKCategoryTypeIdentifierSleepAnalysis",
                "sourceName": "Sample Data Generator",
                "value": "HKCategoryValueSleepAnalysisAsleepCore",
                "startDate": bedtime.strftime(_APPLE_DATETIME_FORMAT),
                "endDate": wake.strftime(_APPLE_DATETIME_FORMAT),
            },
        )
        root.append(sleep_elem)

    xml_bytes = ET.tostring(root, encoding="utf-8")
    pretty = minidom.parseString(xml_bytes).toprettyxml(indent="  ")
    # minidom adds its own XML declaration without our encoding; drop its
    # first line and write one declaration ourselves for a stable, exact
    # byte output across Python versions.
    body = "\n".join(pretty.splitlines()[1:])
    path.write_text('<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n", encoding="utf-8", newline="\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument(
        "--start-date", type=lambda s: date.fromisoformat(s), default=DEFAULT_START_DATE, metavar="YYYY-MM-DD"
    )
    parser.add_argument(
        "--output", type=Path, default=Path(__file__).parent / "health_sample.csv", help="Where to write the CSV."
    )
    parser.add_argument(
        "--xml-output",
        type=Path,
        default=Path(__file__).parent / "apple_health_sample.xml",
        help="Where to write the Apple Health export.xml sample.",
    )
    args = parser.parse_args()

    rows = generate_rows(seed=args.seed, days=args.days, start_date=args.start_date)
    write_csv(rows, args.output)
    write_apple_health_xml(rows, args.xml_output)
    print(f"Wrote {len(rows)} day(s) to {args.output} and {args.xml_output} (seed={args.seed}).")


if __name__ == "__main__":
    main()
