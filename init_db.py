"""
init_db.py
==========
Initializes the local SQLite database used by the Quantified Self MCP
server (server.py), from a health-data export file.

Usage:
    python init_db.py path/to/health.csv
    python init_db.py path/to/export.xml            # Apple Health export
    python init_db.py path/to/health.csv --source csv

By default the source format is guessed from the file extension (--source
auto, the default: .xml -> apple-health, anything else -> this project's
own csv format below) — pass --source explicitly to override that guess.
See import_adapters.py to add support for another export format; adding
one there is all that's needed; nothing below has to change.

By default the database is created at data/health.db next to this script
if that's writable, or wherever the HEALTH_DB_PATH environment variable
points (the same variable server.py reads, so both agree on the location
automatically). If data/ next to this script isn't writable — the usual
case for a system-wide `pip install`, where this script lives inside
site-packages — it falls back to a per-user data directory instead (see
logic.default_data_dir). Pass --db-path to override any of this for a
single run.

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
skipped. All of this applies equally to non-CSV sources (see
import_adapters.py); only the parsing of the source file itself differs.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime
from pathlib import Path

from import_adapters import ADAPTERS, RowError, detect_adapter
from logic import connect_writable, default_data_dir, ensure_schema, upsert_metrics, validate_metrics

BASE_DIR = Path(__file__).parent.resolve()
DATA_DIR = default_data_dir(BASE_DIR)

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


def _to_int(raw: str) -> int | None:
    raw = _clean_number(raw)
    if not raw:
        return None
    try:
        return int(float(raw))
    except ValueError as exc:
        raise RowError(f"expected a number, got {raw!r}") from exc


def _to_float(raw: str) -> float | None:
    raw = _clean_number(raw)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise RowError(f"expected a number, got {raw!r}") from exc


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


def _load_csv_rows(csv_path: Path) -> tuple[list[dict], list[str], int]:
    """This project's own CSV format, exactly as before this module split
    out other adapters — unchanged so existing tests (and existing CSV
    exports people already have from this project) keep working exactly
    as they did. Returns (parsed_rows, present_columns, skipped) to match
    the shape init_health_db needs regardless of which source it read.
    """
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
    return parsed_rows, present_columns, skipped


def init_health_db(source_path: Path, db_path: Path, replace: bool, source: str = "auto") -> None:
    """Import source_path into db_path.

    source picks which import_adapters.ADAPTERS entry reads source_path;
    "auto" (the default) guesses from the file extension via
    import_adapters.detect_adapter, "csv" is always this project's own
    format (handled directly here — see _load_csv_rows), and any other
    name must be a key in import_adapters.ADAPTERS.
    """
    adapter_name = detect_adapter(source_path) if source == "auto" else source

    if adapter_name == "csv":
        parsed_rows, present_columns, skipped = _load_csv_rows(source_path)
    else:
        if adapter_name not in ADAPTERS:
            sys.exit(f"Error: unknown import source {adapter_name!r}. Available: csv, {', '.join(ADAPTERS)}")
        adapted = ADAPTERS[adapter_name](source_path)
        parsed_rows, skipped = [], adapted.skipped
        for row in adapted.rows:
            try:
                validate_metrics({k: v for k, v in row.items() if k != "date"})
                parsed_rows.append(row)
            except ValueError as exc:
                print(f"Skipping {source_path} date {row.get('date')}: {exc}", file=sys.stderr)
                skipped += 1
        present_columns = adapted.present_columns

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
    print(
        f"Health DB ready at {db_path}: {len(parsed_rows)} row(s) loaded, "
        f"{skipped} skipped. (source: {adapter_name})"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Load a health-data export into the Quantified Self health SQLite database."
    )
    parser.add_argument(
        "source_path",
        type=Path,
        help="Path to the source export file (this project's CSV, or e.g. Apple Health's export.xml).",
    )
    parser.add_argument(
        "--source",
        choices=["auto", "csv", *ADAPTERS],
        default="auto",
        help=(
            "Which import format source_path is. 'auto' (default) guesses "
            "from the file extension: .xml -> apple-health, anything else "
            "-> csv. See import_adapters.py for what each non-csv adapter "
            "supports."
        ),
    )
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

    if not args.source_path.exists():
        sys.exit(f"Error: source file not found at {args.source_path}")

    db_path = args.db_path.expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    init_health_db(args.source_path, db_path, args.replace, args.source)


if __name__ == "__main__":
    main()
