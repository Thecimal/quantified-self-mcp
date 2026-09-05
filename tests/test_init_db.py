"""
Unit tests for init_db.py.

Run with: pytest

These cover the CSV-parsing helpers and the CLI end to end. Before this
file, init_db.py was only exercised by CI's single happy-path CSV
(sample_data/health_sample.csv) — edge cases like bad dates, non-numeric
values, and --db-path/--replace had no test coverage at all.
"""

import os
import subprocess
import sqlite3
import sys
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
    row = conn.execute("SELECT steps, sleep_hours, resting_heart_rate FROM daily_metrics").fetchone()
    conn.close()
    assert row == (8000, 7.5, 60)


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
    count = conn.execute("SELECT COUNT(*) FROM daily_metrics").fetchone()[0]
    conn.close()
    assert count == 1


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
    rows = conn.execute("SELECT date FROM daily_metrics").fetchall()
    conn.close()
    assert rows == [("2026-02-01",)]


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
    row = conn.execute("SELECT steps, mood, weight_kg FROM daily_metrics WHERE date = '2026-01-01'").fetchone()
    conn.close()
    assert row == (8000, 4, 70.5)


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
