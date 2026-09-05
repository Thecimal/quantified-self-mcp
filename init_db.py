"""
init_db.py
==========
Initializes the local SQLite database used by the Quantified Self MCP
server (server.py), from a plain CSV file of health data.

Usage:
    python init_db.py path/to/health.csv

By default the database is created at data/health.db next to this script,
or wherever the HEALTH_DB_PATH environment variable points (the same
variable server.py reads, so both agree on the location automatically).
Pass --db-path to override either of those for a single run — handy when
installed via pip, where the default location is inside the installed
package rather than somewhere obviously writable.

Required CSV columns (header names are matched case-insensitively):
    date

The original columns (steps, sleep_hours, resting_heart_rate) and the
newer ones (weight_kg, workout_minutes, mood, water_ml) are all read only
if present in a given CSV's header — this is what makes the incremental
update described below work for any of them, not just the newer ones.

Dates should be YYYY-MM-DD; MM/DD/YYYY is also accepted. Numbers may
include "$" and "," (stripped automatically, kept for consistency with
the shared parsing helpers).

Re-running upserts by date, so it's safe to re-run as you add more days —
or to add a column later: a CSV with only date and weight_kg, say, updates
just that column and leaves steps/sleep_hours/etc. for that date
untouched, rather than blanking them out. Pass --replace to clear the
table first instead. Rows with a problem (bad date, non-numeric value,
etc.) are skipped with a warning rather than aborting the whole import —
the final line printed always tells you how many rows loaded vs. were
skipped.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from logic import connect_writable, ensure_schema, upsert_metrics, validate_metrics

BASE_DIR = Path(__file__).parent.resolve()
DATA_DIR = BASE_DIR / "data"

# Matches server.py's HEALTH_DB_PATH convention, so both halves of the
# project agree on where the database lives without extra configuration.
DEFAULT_DB_PATH = Path(os.environ.get("HEALTH_DB_PATH", DATA_DIR / "health.db")).expanduser()

# "date" is the only column a CSV header must contain. Everything else is
# read only if present in that particular header (see _read_csv) — this
# list is the original set of columns from the project's first release,
# kept separate from METRIC_COLUMNS_ADDED_LATER only for that historical
# reason, not because either group is more "required" than the other.
CORE_METRIC_COLUMNS = ["steps", "sleep_hours", "resting_heart_rate"]

# Columns added after the original release. Kept separate from
# CORE_METRIC_COLUMNS only to document that history; treated identically.
OPTIONAL_COLUMNS = ["weight_kg", "workout_minutes", "mood", "water_ml"]

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


def _read_csv(csv_path: Path) -> tuple[list[dict[str, str]], list[str]]:
    """Read csv_path, matching column names case-insensitively.

    Returns (rows, present_columns) — the latter is whichever metric
    columns (core or added-later) actually appeared in this CSV's header,
    so the caller knows which ones to parse, validate, and upsert versus
    leave untouched. "date" is the only column required to be in the
    header at all; a CSV with just date + one metric column is valid.
    """
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            sys.exit(f"Error: {csv_path} appears to be empty.")
        header_map = {name.strip().lower(): name for name in reader.fieldnames}
        if "date" not in header_map:
            sys.exit(
                f"Error: {csv_path} is missing required column: date. "
                f"Found columns: {', '.join(reader.fieldnames)}"
            )
        present_columns = [c for c in ALL_METRIC_COLUMNS if c in header_map]
        wanted = {"date": header_map["date"], **{c: header_map[c] for c in present_columns}}
        rows = []
        for raw_row in reader:
            rows.append({canonical: raw_row.get(original) for canonical, original in wanted.items()})
    return rows, present_columns


ALL_METRIC_COLUMNS = CORE_METRIC_COLUMNS + OPTIONAL_COLUMNS

# Parser for each metric column's raw CSV string.
_METRIC_PARSERS = {
    "steps": _to_int,
    "sleep_hours": _to_float,
    "resting_heart_rate": _to_int,
    "weight_kg": _to_float,
    "workout_minutes": _to_int,
    "mood": _to_int,
    "water_ml": _to_int,
}


def init_health_db(csv_path: Path, db_path: Path, replace: bool) -> None:
    raw_rows, present_columns = _read_csv(csv_path)

    parsed_rows, skipped = [], 0
    for i, row in enumerate(raw_rows, start=2):  # +2: header is line 1
        try:
            date_val = (row.get("date") or "").strip()
            if not date_val:
                raise RowError("missing date")
            parsed = {"date": _normalize_date(date_val)}
            for col in present_columns:
                parsed[col] = _METRIC_PARSERS[col](row.get(col) or "")
            try:
                validate_metrics({k: v for k, v in parsed.items() if k != "date"})
            except ValueError as exc:
                raise RowError(str(exc)) from exc
            parsed_rows.append(parsed)
        except RowError as exc:
            print(f"Skipping {csv_path} line {i}: {exc}", file=sys.stderr)
            skipped += 1

    conn = connect_writable(db_path)
    try:
        ensure_schema(conn)
        if replace:
            conn.execute("DELETE FROM daily_metrics")
        upsert_metrics(conn, parsed_rows)
    finally:
        conn.close()

    if present_columns:
        print(f"Loaded columns: {', '.join(present_columns)}")
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
    parser.add_argument(
        "--db-path",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=(
            "Where to create/update the SQLite database. Defaults to the "
            "HEALTH_DB_PATH environment variable if set (same variable "
            "server.py reads), otherwise data/health.db next to this script "
            f"(currently: {DEFAULT_DB_PATH})."
        ),
    )
    args = parser.parse_args()

    if not args.csv_path.exists():
        sys.exit(f"Error: CSV file not found at {args.csv_path}")

    db_path = args.db_path.expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    init_health_db(args.csv_path, db_path, args.replace)


if __name__ == "__main__":
    main()
