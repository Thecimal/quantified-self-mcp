"""
Tests for sample_data/generate_sample_data.py.

The core claim of issue #28 is reproducibility: regenerating the checked-in
sample files with the documented default seed must produce them
byte-for-byte, not just "similar-looking" data. test_regenerating_the_
default_* below are the direct check of that; the rest cover that the
generator's output actually satisfies validate_metrics and imports
cleanly, since generated-but-unusable sample data would defeat the point.
"""

import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "sample_data"))

from generate_sample_data import (  # noqa: E402
    DEFAULT_DAYS,
    DEFAULT_SEED,
    DEFAULT_START_DATE,
    generate_rows,
    write_apple_health_xml,
    write_csv,
)

from logic import validate_metrics  # noqa: E402

CHECKED_IN_CSV = REPO_ROOT / "sample_data" / "health_sample.csv"
CHECKED_IN_XML = REPO_ROOT / "sample_data" / "apple_health_sample.xml"


def test_regenerating_the_default_csv_matches_the_checked_in_file(tmp_path):
    rows = generate_rows(seed=DEFAULT_SEED, days=DEFAULT_DAYS, start_date=DEFAULT_START_DATE)
    out = tmp_path / "health_sample.csv"
    write_csv(rows, out)
    assert out.read_text(encoding="utf-8") == CHECKED_IN_CSV.read_text(encoding="utf-8")


def test_regenerating_the_default_apple_health_xml_matches_the_checked_in_file(tmp_path):
    rows = generate_rows(seed=DEFAULT_SEED, days=DEFAULT_DAYS, start_date=DEFAULT_START_DATE)
    out = tmp_path / "apple_health_sample.xml"
    write_apple_health_xml(rows, out)
    assert out.read_text(encoding="utf-8") == CHECKED_IN_XML.read_text(encoding="utf-8")


def test_generate_rows_is_deterministic_for_a_given_seed():
    assert generate_rows(seed=123, days=10) == generate_rows(seed=123, days=10)


def test_generate_rows_differs_for_a_different_seed():
    assert generate_rows(seed=1, days=10) != generate_rows(seed=2, days=10)


def test_generate_rows_covers_the_requested_date_range():
    rows = generate_rows(seed=DEFAULT_SEED, days=5, start_date=date(2026, 1, 1))
    assert [row["date"] for row in rows] == [date(2026, 1, 1) + timedelta(days=i) for i in range(5)]


def test_every_generated_row_passes_validate_metrics():
    for row in generate_rows(seed=DEFAULT_SEED, days=DEFAULT_DAYS, start_date=DEFAULT_START_DATE):
        validate_metrics({k: v for k, v in row.items() if k != "date"})


def test_some_generated_rows_omit_an_optional_metric():
    """The sample data is supposed to demonstrate — not just describe —
    that every field is optional per day (see README.md); if every row
    happened to have every field, that claim would go untested by the
    sample data itself.
    """
    rows = generate_rows(seed=DEFAULT_SEED, days=DEFAULT_DAYS, start_date=DEFAULT_START_DATE)
    optional = ("weight_kg", "workout_minutes", "mood", "water_ml")
    assert any(any(col not in row for col in optional) for row in rows)


def test_cli_regenerates_files_matching_the_checked_in_defaults(tmp_path):
    csv_out = tmp_path / "health_sample.csv"
    xml_out = tmp_path / "apple_health_sample.xml"
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "sample_data" / "generate_sample_data.py"),
            "--output",
            str(csv_out),
            "--xml-output",
            str(xml_out),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert csv_out.read_text(encoding="utf-8") == CHECKED_IN_CSV.read_text(encoding="utf-8")
    assert xml_out.read_text(encoding="utf-8") == CHECKED_IN_XML.read_text(encoding="utf-8")


def test_generated_csv_imports_cleanly_through_init_db(tmp_path):
    db_path = tmp_path / "health.db"
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "init_db.py"), str(CHECKED_IN_CSV), "--db-path", str(db_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "0 skipped" in result.stdout
    assert f"{DEFAULT_DAYS} row(s) loaded" in result.stdout


def test_generated_apple_health_xml_imports_cleanly_through_init_db(tmp_path):
    db_path = tmp_path / "health.db"
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "init_db.py"), str(CHECKED_IN_XML), "--db-path", str(db_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "0 skipped" in result.stdout
    assert "source: apple-health" in result.stdout


def test_a_different_seed_produces_a_different_file(tmp_path):
    out = tmp_path / "different.csv"
    rows = generate_rows(seed=DEFAULT_SEED + 1, days=DEFAULT_DAYS, start_date=DEFAULT_START_DATE)
    write_csv(rows, out)
    assert out.read_text(encoding="utf-8") != CHECKED_IN_CSV.read_text(encoding="utf-8")


@pytest.mark.parametrize("days", [1, 7, 90])
def test_generate_rows_handles_various_lengths(days):
    rows = generate_rows(seed=DEFAULT_SEED, days=days, start_date=DEFAULT_START_DATE)
    assert len(rows) == days
