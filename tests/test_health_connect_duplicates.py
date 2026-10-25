"""
Health Connect import: what counts as the same record twice, and what must never be merged.

A Health Connect record carries metadata.id. The same recordType and id repeated with identical content is one
record exported twice (overlapping pages, concatenated exports) and must count once; otherwise a "sum" metric such as
steps is inflated. Everything ambiguous stays: records without an id, equal values at different times, equal values
from different data origins, one id reused by another record type, and one id whose content differs. The last is kept
but reported, so it is never dropped silently.

Each day below is one case, checked through the real import pipeline (file -> adapter -> measurements -> daily_metrics).
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json  # noqa: E402

from init_db import init_health_db  # noqa: E402

GOOGLE_FIT = "com.google.android.apps.fitness"
SAMSUNG = "com.sec.android.app.shealth"


def _steps(day, hour, count, record_id=None, origin=None):
    record = {
        "recordType": "StepsRecord",
        "startTime": f"2026-06-{day:02d}T{hour:02d}:00:00Z",
        "endTime": f"2026-06-{day:02d}T{hour:02d}:10:00Z",
        "count": count,
    }
    metadata = {}
    if record_id is not None:
        metadata["id"] = record_id
    if origin is not None:
        metadata["dataOrigin"] = {"packageName": origin}
    if metadata:
        record["metadata"] = metadata
    return record


RECORDS = [
    # 06-01: one record, same id and identical content, exported twice -> counted once.
    _steps(1, 8, 1000, "a1"),
    _steps(1, 8, 1000, "a1"),
    # 06-02: identical records with no id cannot be told apart from two real readings -> both kept.
    _steps(2, 8, 700),
    _steps(2, 8, 700),
    # 06-03: equal values at different times with different ids are two real readings -> both kept.
    _steps(3, 8, 500, "c1"),
    _steps(3, 9, 500, "c2"),
    # 06-04: two data origins reporting the same interval and value. Matching values alone do not make them one
    # record, so neither is dropped here (which origin should win is a source-resolution question, not this one's).
    _steps(4, 8, 2000, "d1", GOOGLE_FIT),
    _steps(4, 8, 2000, "d2", SAMSUNG),
    # 06-05: one id, different content (a record edited between exports): ambiguous, so both are kept.
    _steps(5, 8, 300, "e1"),
    _steps(5, 8, 400, "e1"),
    # 06-06: the same id on two different record types is not a repeat.
    _steps(6, 8, 1000, "f1"),
    {
        "recordType": "WeightRecord",
        "time": "2026-06-06T07:00:00Z",
        "weight": {"value": 70.0, "unit": "kilograms"},
        "metadata": {"id": "f1"},
    },
]


def _import(tmp_path):
    path = tmp_path / "health_connect.json"
    path.write_text(json.dumps(RECORDS), encoding="utf-8")
    db = tmp_path / "health.db"
    init_health_db(path, db, replace=False, source="health-connect")
    with sqlite3.connect(db) as conn:
        return {
            (date, metric): value
            for date, metric, value in conn.execute("SELECT date, metric, value FROM daily_metrics")
        }


def test_a_repeated_health_connect_record_counts_once_and_ambiguous_ones_are_kept(tmp_path, capsys):
    daily = _import(tmp_path)

    assert daily[("2026-06-01", "steps")] == 1000  # identical repeat of a1: once
    assert daily[("2026-06-02", "steps")] == 1400  # no id: both kept
    assert daily[("2026-06-03", "steps")] == 1000  # equal values, different ids and times: both kept
    assert daily[("2026-06-04", "steps")] == 4000  # different origins, matching values: not merged
    assert daily[("2026-06-05", "steps")] == 700  # same id, different content: both kept
    assert daily[("2026-06-06", "steps")] == 1000  # id shared with another record type: not a repeat
    assert daily[("2026-06-06", "weight_kg")] == 70.0

    # only the conflicting-content id is reported (not silently dropped); a clean repeat and a cross-type id are not
    warnings = capsys.readouterr().err
    assert "e1" in warnings
    assert "a1" not in warnings and "f1" not in warnings
