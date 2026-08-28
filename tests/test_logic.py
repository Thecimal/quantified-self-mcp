"""
Unit tests for logic.py.

Run with: pytest

These only exercise the framework-free helpers in logic.py, so they run
without fastmcp installed — server.py itself is not imported here.
"""

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logic import numeric_stats, parse_date, resolve_range  # noqa: E402


def test_parse_date_valid():
    assert parse_date("2026-08-23", "start_date") == date(2026, 8, 23)


def test_parse_date_rejects_wrong_format():
    with pytest.raises(ValueError, match="start_date must be formatted YYYY-MM-DD"):
        parse_date("08/23/2026", "start_date")


def test_resolve_range_defaults_to_today_and_default_days():
    start, end = resolve_range(None, None, default_days=30)
    assert end == date.today()
    assert (end - start).days == 30


def test_resolve_range_explicit_dates():
    start, end = resolve_range("2026-01-01", "2026-01-31", default_days=30)
    assert start == date(2026, 1, 1)
    assert end == date(2026, 1, 31)


def test_resolve_range_rejects_start_after_end():
    with pytest.raises(ValueError, match="is after end_date"):
        resolve_range("2026-02-01", "2026-01-01", default_days=30)


def test_resolve_range_rejects_absurdly_wide_range():
    with pytest.raises(ValueError, match="over the"):
        resolve_range("2000-01-01", "2026-01-01", default_days=30)


def test_numeric_stats_empty():
    assert numeric_stats([], "steps") == {"avg": None, "min": None, "max": None}


def test_numeric_stats_ignores_none_but_keeps_zero():
    rows = [{"steps": 0}, {"steps": None}, {"steps": 10000}]
    assert numeric_stats(rows, "steps") == {"avg": 5000.0, "min": 0, "max": 10000}
