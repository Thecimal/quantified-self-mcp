"""
init_db.py
==========
Initializes the local SQLite database used by the Quantified Self MCP
server (server.py), from a plain CSV file of health data.

Usage:
    python init_db.py path/to/health.csv

Expected CSV columns (header names are matched case-insensitively):
    date, steps, sleep_hours, resting_heart_rate

Dates should be YYYY-MM-DD; MM/DD/YYYY is also accepted. Numbers may
include "$" and "," (stripped automatically, kept for consistency with
the shared parsing helpers).

Re-running upserts by date, so it's safe to re-run as you add more days.
Pass --replace to clear the table first instead. Rows with a problem (bad
date, non-numeric value, etc.) are skipped with a warning rather than
aborting the whole import — the final line printed always tells you how
many rows loaded vs. were skipped.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from logic import HEALTH_SCHEMA

BASE_DIR = Path(__file__).parent.resolve()
DATA_DIR = BASE_DIR / "data"

REQUIRED_COLUMNS = ["date", "steps", "sleep_hours", "resting_heart_rate"]

DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y"]


class RowError(ValueError):
    """A single CSV row couldn't be parsed; the import continues without it."""


def _normalize_date(raw: str) -> str:
    raw = raw.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    raise RowError(f"unrecognized date {raw!r} (use YYYY-MM-DD or MM/DD/YYYY)")


def _clean_number(raw: str) -> str:
    return raw.strip().replace("$", "").replace(",", "")


def _to_int(raw: str) -> Optional[int]:
    raw = _clean_number(raw)
    if not raw:
        return None
    try:
        return int(float(raw))
    except ValueError:
        raise RowError(f"expected a number, got {raw!r}")


def _to_float(raw: str) -> Optional[float]:
    raw = _clean_number(raw)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        raise RowError(f"expected a number, got {raw!r}")


def _read_csv(csv_path: Path) -> list[dict[str, str]]:
    """Read csv_path, matching required column names case-insensitively."""
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            sys.exit(f"Error: {csv_path} appears to be empty.")
        header_map = {name.strip().lower(): name for name in reader.fieldnames}
        missing = [c for c in REQUIRED_COLUMNS if c not in header_map]
        if missing:
            sys.exit(
                f"Error: {csv_path} is missing required column(s): {', '.join(missing)}. "
                f"Found columns: {', '.join(reader.fieldnames)}"
            )
        rows = []
        for raw_row in reader:
            rows.append({canonical: raw_row.get(original) for canonical, original in header_map.items()})
    return rows


def init_health_db(csv_path: Path, db_path: Path, replace: bool) -> None:
    raw_rows = _read_csv(csv_path)
    parsed_rows, skipped = [], 0
    for i, row in enumerate(raw_rows, start=2):  # +2: header is line 1
        try:
            date_val = (row.get("date") or "").strip()
            if not date_val:
                raise RowError("missing date")
            parsed_rows.append(
                {
                    "date": _normalize_date(date_val),
                    "steps": _to_int(row.get("steps") or ""),
                    "sleep_hours": _to_float(row.get("sleep_hours") or ""),
                    "resting_heart_rate": _to_int(row.get("resting_heart_rate") or ""),
                }
            )
        except RowError as exc:
            print(f"Skipping {csv_path} line {i}: {exc}", file=sys.stderr)
            skipped += 1

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(HEALTH_SCHEMA)
        if replace:
            conn.execute("DELETE FROM daily_metrics")
        conn.executemany(
            """
            INSERT INTO daily_metrics (date, steps, sleep_hours, resting_heart_rate)
            VALUES (:date, :steps, :sleep_hours, :resting_heart_rate)
            ON CONFLICT(date) DO UPDATE SET
                steps = excluded.steps,
                sleep_hours = excluded.sleep_hours,
                resting_heart_rate = excluded.resting_heart_rate
            """,
            parsed_rows,
        )
        conn.commit()
    finally:
        conn.close()
    print(f"Health DB ready at {db_path}: {len(parsed_rows)} row(s) loaded, {skipped} skipped.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Load a CSV export into the Quantified Self health SQLite database."
    )
    parser.add_argument("csv_path", type=Path, help="Path to the source health CSV file.")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Clear existing rows in the table first, instead of appending/upserting.",
    )
    args = parser.parse_args()

    if not args.csv_path.exists():
        sys.exit(f"Error: CSV file not found at {args.csv_path}")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    init_health_db(args.csv_path, DATA_DIR / "health.db", args.replace)


if __name__ == "__main__":
    main()
