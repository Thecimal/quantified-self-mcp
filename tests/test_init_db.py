"""
Unit tests for init_db.py.

Run with: pytest

These cover the CSV-parsing helpers and the CLI end to end. Before this
file, init_db.py was only exercised by CI's single happy-path CSV
(sample_data/health_sample.csv) — edge cases like bad dates, non-numeric
values, and --db-path/--replace had no test coverage at all.
"""

import os
import sqlite3
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from init_db import (
    RowError,
    _normalize_date,
    _read_csv,
    _to_float,
    _to_int,
    init_health_db,
)  # noqa: E402
from logic import daily_metrics_wide  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_CSV = REPO_ROOT / "sample_data" / "health_sample.csv"


# ---------------------------------------------------------------------------
# _normalize_date
# ---------------------------------------------------------------------------


def test_normalize_date_accepts_iso_format():
    assert _normalize_date("2026-01-05") == "2026-01-05"


def test_normalize_date_accepts_us_slash_format():
    assert _normalize_date("01/05/2026") == "2026-01-05"


def test_normalize_date_strips_whitespace():
    assert _normalize_date("  2026-01-05  ") == "2026-01-05"


def test_normalize_date_rejects_unrecognized_format():
    with pytest.raises(RowError, match="unrecognized date"):
        _normalize_date("Jan 5 2026")


# ---------------------------------------------------------------------------
# _to_int / _to_float
# ---------------------------------------------------------------------------


def test_to_int_parses_plain_number():
    assert _to_int("8000") == 8000


def test_to_int_strips_currency_and_commas():
    assert _to_int("$1,234") == 1234


def test_to_int_empty_string_is_none():
    assert _to_int("") is None
    assert _to_int("   ") is None


def test_to_int_rejects_non_numeric():
    with pytest.raises(RowError, match="expected a number"):
        _to_int("not-a-number")


def test_to_float_parses_decimal():
    assert _to_float("70.5") == 70.5


def test_to_float_empty_string_is_none():
    assert _to_float("") is None


def test_to_float_rejects_non_numeric():
    with pytest.raises(RowError, match="expected a number"):
        _to_float("heavy")


# ---------------------------------------------------------------------------
# _read_csv
# ---------------------------------------------------------------------------


def _write_csv(tmp_path, header, rows):
    path = tmp_path / "input.csv"
    lines = [",".join(header)] + [",".join(row) for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_read_csv_matches_headers_case_insensitively(tmp_path):
    csv_path = _write_csv(
        tmp_path,
        ["Date", "STEPS", "Sleep_Hours", "resting_heart_rate"],
        [["2026-01-01", "8000", "7.5", "60"]],
    )
    rows, present_columns = _read_csv(csv_path)
    assert present_columns == ["steps", "sleep_hours", "resting_heart_rate"]
    assert rows[0]["date"] == "2026-01-01"
    assert rows[0]["steps"] == "8000"


def test_read_csv_detects_present_columns(tmp_path):
    csv_path = _write_csv(
        tmp_path,
        ["date", "steps", "sleep_hours", "resting_heart_rate", "mood"],
        [["2026-01-01", "8000", "7.5", "60", "4"]],
    )
    _, present_columns = _read_csv(csv_path)
    assert present_columns == ["steps", "sleep_hours", "resting_heart_rate", "mood"]


def test_read_csv_exits_on_missing_date_column(tmp_path):
    csv_path = _write_csv(
        tmp_path,
        ["steps", "sleep_hours", "resting_heart_rate"],  # no date column at all
        [["8000", "7.5", "60"]],
    )
    with pytest.raises(SystemExit, match="missing required column: date"):
        _read_csv(csv_path)


def test_read_csv_accepts_header_missing_core_columns(tmp_path):
    """Only 'date' is required in the header — steps/sleep_hours/resting_heart_rate
    are read only if present, exactly like weight_kg/mood/etc. This is what makes
    the "add one column later" workflow described in the module docstring work."""
    csv_path = _write_csv(tmp_path, ["date", "weight_kg"], [["2026-01-01", "70.5"]])
    rows, present_columns = _read_csv(csv_path)
    assert present_columns == ["weight_kg"]
    assert rows[0] == {"date": "2026-01-01", "weight_kg": "70.5"}


def test_read_csv_exits_on_empty_file(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")
    with pytest.raises(SystemExit, match="appears to be empty"):
        _read_csv(path)


def test_read_csv_column_map_resolves_differently_named_headers(tmp_path):
    csv_path = _write_csv(
        tmp_path,
        ["Date", "Daily Steps", "Sleep Duration"],
        [["2026-01-01", "8000", "7.5"]],
    )
    rows, present_columns = _read_csv(
        csv_path, column_map={"date": "Date", "steps": "Daily Steps", "sleep_hours": "Sleep Duration"}
    )
    assert present_columns == ["steps", "sleep_hours"]
    assert rows[0] == {"date": "2026-01-01", "steps": "8000", "sleep_hours": "7.5"}


def test_read_csv_column_map_only_overrides_mapped_columns(tmp_path):
    """Columns not mentioned in column_map still fall back to the normal
    case-insensitive match."""
    csv_path = _write_csv(
        tmp_path,
        ["Date", "Daily Steps", "mood"],
        [["2026-01-01", "8000", "4"]],
    )
    rows, present_columns = _read_csv(csv_path, column_map={"date": "Date", "steps": "Daily Steps"})
    assert present_columns == ["steps", "mood"]
    assert rows[0]["mood"] == "4"


def test_read_csv_column_map_exits_on_unmatched_header(tmp_path):
    csv_path = _write_csv(tmp_path, ["Date", "Steps"], [["2026-01-01", "8000"]])
    with pytest.raises(SystemExit, match="no such"):
        _read_csv(csv_path, column_map={"date": "Date", "steps": "Nonexistent Column"})


def test_read_csv_column_map_covers_missing_date_column(tmp_path):
    csv_path = _write_csv(tmp_path, ["Day", "Steps"], [["2026-01-01", "8000"]])
    rows, present_columns = _read_csv(csv_path, column_map={"date": "Day"})
    assert present_columns == ["steps"]
    assert rows[0]["date"] == "2026-01-01"


# ---------------------------------------------------------------------------
# _read_csv — COLUMN_ALIASES (alternate header spellings, no --map needed)
# ---------------------------------------------------------------------------


def test_read_csv_recognizes_step_count_alias(tmp_path):
    csv_path = _write_csv(tmp_path, ["date", "step_count"], [["2026-01-01", "8000"]])
    rows, present_columns = _read_csv(csv_path)
    assert present_columns == ["steps"]
    assert rows[0]["steps"] == "8000"


def test_read_csv_recognizes_alias_case_insensitively(tmp_path):
    csv_path = _write_csv(tmp_path, ["Date", "STEP_COUNT"], [["2026-01-01", "8000"]])
    rows, present_columns = _read_csv(csv_path)
    assert present_columns == ["steps"]
    assert rows[0]["steps"] == "8000"


@pytest.mark.parametrize(
    ("canonical", "alias_header"),
    [
        ("steps", "daily_steps"),
        ("sleep_hours", "sleep"),
        ("sleep_hours", "sleep_duration"),
        ("resting_heart_rate", "rhr"),
        ("heart_rate", "bpm"),
        ("hrv_ms", "hrv"),
        ("weight_kg", "weight"),
        ("weight_kg", "body_weight"),
        ("workout_minutes", "exercise_minutes"),
        ("water_ml", "water"),
        ("mood", "mood_score"),
    ],
)
def test_read_csv_recognizes_each_documented_alias(tmp_path, canonical, alias_header):
    csv_path = _write_csv(tmp_path, ["date", alias_header], [["2026-01-01", "5"]])
    rows, present_columns = _read_csv(csv_path)
    assert present_columns == [canonical]
    assert rows[0][canonical] == "5"


def test_read_csv_canonical_name_wins_over_alias_when_both_present(tmp_path):
    """If a CSV happens to have both the canonical column and an alias
    column, the canonical one is used — aliases only fill in when the
    canonical name isn't present at all."""
    csv_path = _write_csv(
        tmp_path, ["date", "steps", "step_count"], [["2026-01-01", "8000", "9999"]]
    )
    rows, present_columns = _read_csv(csv_path)
    assert present_columns == ["steps"]
    assert rows[0]["steps"] == "8000"


def test_read_csv_column_map_overrides_alias(tmp_path):
    """An explicit --map for a column still wins even if the CSV also
    has a header that would otherwise match that column's alias list."""
    csv_path = _write_csv(
        tmp_path, ["date", "step_count", "Really Daily Steps"], [["2026-01-01", "1111", "8000"]]
    )
    rows, present_columns = _read_csv(csv_path, column_map={"steps": "Really Daily Steps"})
    assert present_columns == ["steps"]
    assert rows[0]["steps"] == "8000"


# ---------------------------------------------------------------------------
# init_health_db
# ---------------------------------------------------------------------------


def test_init_health_db_loads_valid_rows(tmp_path):
    csv_path = _write_csv(
        tmp_path,
        ["date", "steps", "sleep_hours", "resting_heart_rate"],
        [["2026-01-01", "8000", "7.5", "60"]],
    )
    db_path = tmp_path / "health.db"
    init_health_db(csv_path, db_path, replace=False)

    conn = sqlite3.connect(db_path)
    rows = daily_metrics_wide(conn, ["steps", "sleep_hours", "resting_heart_rate"])
    conn.close()
    assert rows == [{"date": "2026-01-01", "steps": 8000, "sleep_hours": 7.5, "resting_heart_rate": 60}]


def test_init_health_db_skips_bad_rows_but_loads_the_rest(tmp_path):
    csv_path = _write_csv(
        tmp_path,
        ["date", "steps", "sleep_hours", "resting_heart_rate"],
        [
            ["2026-01-01", "8000", "7.5", "60"],
            ["not-a-date", "8000", "7.5", "60"],  # bad date
            ["2026-01-03", "oops", "7.5", "60"],  # bad steps
        ],
    )
    db_path = tmp_path / "health.db"
    init_health_db(csv_path, db_path, replace=False)

    conn = sqlite3.connect(db_path)
    rows = daily_metrics_wide(conn, ["steps", "sleep_hours", "resting_heart_rate"])
    conn.close()
    assert len(rows) == 1


def test_init_health_db_replace_clears_existing_rows_first(tmp_path):
    csv_path = _write_csv(
        tmp_path,
        ["date", "steps", "sleep_hours", "resting_heart_rate"],
        [["2026-01-01", "8000", "7.5", "60"]],
    )
    db_path = tmp_path / "health.db"
    init_health_db(csv_path, db_path, replace=False)

    second_csv = _write_csv(
        tmp_path, ["date", "steps", "sleep_hours", "resting_heart_rate"], [["2026-02-01", "9000", "8.0", "58"]]
    )
    # second_csv overwrites input.csv on disk, which is fine — init_health_db
    # only reads it once, synchronously, above.

    init_health_db(second_csv, db_path, replace=True)

    conn = sqlite3.connect(db_path)
    rows = daily_metrics_wide(conn, ["steps", "sleep_hours", "resting_heart_rate"])
    conn.close()
    assert [r["date"] for r in rows] == ["2026-02-01"]


def test_init_health_db_upserts_optional_columns_without_clobbering_others(tmp_path):
    db_path = tmp_path / "health.db"
    first = _write_csv(
        tmp_path,
        ["date", "steps", "sleep_hours", "resting_heart_rate", "mood"],
        [["2026-01-01", "8000", "7.5", "60", "4"]],
    )
    init_health_db(first, db_path, replace=False)

    second = tmp_path / "second.csv"
    second.write_text("date,weight_kg\n2026-01-01,70.5\n", encoding="utf-8")
    init_health_db(second, db_path, replace=False)

    conn = sqlite3.connect(db_path)
    rows = daily_metrics_wide(conn, ["steps", "mood", "weight_kg"], "2026-01-01", "2026-01-01")
    conn.close()
    assert rows == [{"date": "2026-01-01", "steps": 8000, "mood": 4, "weight_kg": 70.5}]


# ---------------------------------------------------------------------------
# --source / import adapters (#17)
# ---------------------------------------------------------------------------


def test_init_health_db_auto_detects_apple_health_from_xml_extension(tmp_path):
    export = tmp_path / "export.xml"
    export.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<HealthData locale="en_US">\n'
        '<Record type="HKQuantityTypeIdentifierStepCount" unit="count" '
        'startDate="2026-01-15 08:00:00 -0500" endDate="2026-01-15 08:05:00 -0500" value="4000"/>\n'
        "</HealthData>\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "health.db"

    init_health_db(export, db_path, replace=False)  # source="auto" by default

    conn = sqlite3.connect(db_path)
    rows = daily_metrics_wide(conn, ["steps"], "2026-01-15", "2026-01-15")
    conn.close()
    assert rows == [{"date": "2026-01-15", "steps": 4000}]


def test_init_health_db_writes_raw_measurements_with_provenance_from_apple_health(tmp_path):
    export = tmp_path / "export.xml"
    export.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<HealthData locale="en_US">\n'
        '<Record type="HKQuantityTypeIdentifierStepCount" sourceName="Ben\'s iPhone" unit="count" '
        'startDate="2026-01-15 08:00:00 -0500" endDate="2026-01-15 08:05:00 -0500" value="4000"/>\n'
        "</HealthData>\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "health.db"

    init_health_db(export, db_path, replace=False)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = dict(conn.execute("SELECT * FROM measurements WHERE metric = 'steps'").fetchone())
    conn.close()
    assert row["value"] == 4000
    assert row["source"] == "Ben's iPhone"
    assert row["importer"] == "apple-health"
    assert row["imported_at"] is not None


def test_init_health_db_from_csv_writes_measurements_tagged_with_the_csv_importer(tmp_path):
    # Before the measurements/daily_metrics invariant work, a CSV row
    # went straight into the (then directly-writable) wide daily_metrics
    # table and never touched measurements at all. Now daily_metrics is
    # entirely derived from measurements (see db/schema.sql,
    # db/invariant.py), so a CSV import has to write there too — tagged
    # importer="csv" so it's identifiable and so a later --replace only
    # touches CSV-sourced rows, not manually logged or other-importer data.
    csv_path = tmp_path / "health.csv"
    csv_path.write_text("date,steps\n2026-01-15,4000\n", encoding="utf-8")
    db_path = tmp_path / "health.db"

    init_health_db(csv_path, db_path, replace=False)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = dict(conn.execute("SELECT * FROM measurements WHERE metric = 'steps'").fetchone())
    conn.close()
    assert row["value"] == 4000
    assert row["importer"] == "csv"


def test_init_health_db_explicit_source_overrides_extension_guess(tmp_path):
    # A .csv file force-read as apple-health should fail to parse as XML
    # rather than silently falling back to the CSV reader.
    csv_path = _write_csv(tmp_path, ["date", "steps"], [["2026-01-01", "8000"]])
    db_path = tmp_path / "health.db"
    with pytest.raises(ET.ParseError):
        init_health_db(csv_path, db_path, replace=False, source="apple-health")


def test_cli_source_flag_imports_an_apple_health_export(tmp_path):
    export = tmp_path / "export.xml"
    export.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<HealthData locale="en_US">\n'
        '<Record type="HKQuantityTypeIdentifierStepCount" unit="count" '
        'startDate="2026-01-15 08:00:00 -0500" endDate="2026-01-15 08:05:00 -0500" value="4000"/>\n'
        "</HealthData>\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "health.db"
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "init_db.py"), str(export), "--db-path", str(db_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "source: apple-health" in result.stdout
    conn = sqlite3.connect(db_path)
    rows = daily_metrics_wide(conn, ["steps"], "2026-01-15", "2026-01-15")
    conn.close()
    assert rows == [{"date": "2026-01-15", "steps": 4000}]


# ---------------------------------------------------------------------------
# CLI (subprocess, so it exercises argument parsing exactly as a user would)
# ---------------------------------------------------------------------------


def test_cli_db_path_flag_overrides_default_location(tmp_path):
    db_path = tmp_path / "custom" / "health.db"
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "init_db.py"), str(SAMPLE_CSV), "--db-path", str(db_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert db_path.exists()
    assert str(db_path) in result.stdout


def test_cli_health_db_path_env_var_overrides_default_location(tmp_path):
    db_path = tmp_path / "envdir" / "health.db"
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "init_db.py"), str(SAMPLE_CSV)],
        capture_output=True,
        text=True,
        env={**_minimal_env(), "HEALTH_DB_PATH": str(db_path)},
    )
    assert result.returncode == 0
    assert db_path.exists()


def test_cli_map_flag_imports_csv_with_custom_headers(tmp_path):
    csv_path = _write_csv(
        tmp_path,
        ["Date", "Daily Steps", "Sleep Duration"],
        [["2026-01-01", "8000", "7.5"]],
    )
    db_path = tmp_path / "health.db"
    result = subprocess.run(
        [
            sys.executable, str(REPO_ROOT / "init_db.py"), str(csv_path), "--db-path", str(db_path),
            "--map", "date=Date", "--map", "steps=Daily Steps", "--map", "sleep_hours=Sleep Duration",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    conn = sqlite3.connect(db_path)
    rows = daily_metrics_wide(conn, ["steps", "sleep_hours"], "2026-01-01", "2026-01-01")
    conn.close()
    assert rows == [{"date": "2026-01-01", "steps": 8000, "sleep_hours": 7.5}]


def test_cli_map_flag_rejects_unrecognized_column(tmp_path):
    csv_path = _write_csv(tmp_path, ["Date", "Steps"], [["2026-01-01", "8000"]])
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "init_db.py"), str(csv_path), "--map", "not_a_column=Steps"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "not recognized" in result.stderr


def test_cli_exits_cleanly_on_missing_csv(tmp_path):
    missing = tmp_path / "does-not-exist.csv"
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "init_db.py"), str(missing)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "not found" in result.stderr.lower() or "not found" in result.stdout.lower()


def _minimal_env():
    # Keep PATH etc. so the subprocess's Python can actually run.
    return dict(os.environ)
