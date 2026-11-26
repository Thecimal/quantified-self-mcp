"""
Regressions found by the agent permission evaluation (eval/agent/permissions.py), on the permission_limited
fixture: weight_kg and heart_rate are private, and one day (PRIVATE_ONLY_OFFSET days back) holds only a
private weight reading.

Two tools still derived output from every metric, private ones included, and so revealed when a private
metric was logged — the same class of leak read_health_data had before 43a9b25:

* export_health_data_csv wrote an all-blank row for a day that only has a private metric, and counted it
  in rows_exported.
* get_import_status / get_data_status returned the span recorded at import time (computed over all
  metrics), whose first day is that private-only day.
"""

import csv
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from fastmcp import Client

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from eval.agent.fixtures import FIXTURES, PRIVATE_ONLY_OFFSET, build_fixture  # noqa: E402
from eval.agent.runner import loaded_server  # noqa: E402

TODAY = date(2026, 11, 20)
PRIVATE_ONLY_DAY = (TODAY - timedelta(days=PRIVATE_ONLY_OFFSET)).isoformat()
WINDOW = {"start_date": (TODAY - timedelta(days=120)).isoformat(), "end_date": TODAY.isoformat()}
COVERAGE_FIELDS = ("coverage_before_start", "coverage_before_end", "coverage_after_start", "coverage_after_end")
PRIVATE = "weight_kg,heart_rate"


async def _call(tmp_path, private_fields, *calls):
    """Build the permission_limited fixture and make the given (tool, arguments) calls on the real server."""
    db_path = tmp_path / "health.db"
    build_fixture(FIXTURES["permission_limited"], db_path, TODAY)
    results = []
    with loaded_server(db_path, private_fields) as server:
        async with Client(server.mcp) as client:
            for tool, arguments in calls:
                results.append((await client.call_tool(tool, arguments)).structured_content)
    return results


def _exported_rows(export):
    with Path(export["path"]).open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


async def test_export_omits_days_that_only_have_a_private_metric(tmp_path):
    read, export = await _call(tmp_path, PRIVATE, ("read_health_data", WINDOW), ("export_health_data_csv", WINDOW))
    rows = _exported_rows(export)
    assert export["rows_exported"] == len(rows) == read["summary"]["days_with_data"] == 90
    assert PRIVATE_ONLY_DAY not in {row["date"] for row in rows}
    assert all(row["weight_kg"] == "" and row["heart_rate"] == "" for row in rows)
    assert any(row["steps"] != "" for row in rows)  # public columns are still exported


async def test_export_lists_the_private_only_day_when_nothing_is_private(tmp_path):
    (export,) = await _call(tmp_path, "", ("export_health_data_csv", WINDOW))
    assert PRIVATE_ONLY_DAY in {row["date"] for row in _exported_rows(export)}
    assert export["rows_exported"] == 91


async def test_import_records_do_not_expose_the_dataset_span_when_a_metric_is_private(tmp_path):
    imports, status = await _call(tmp_path, PRIVATE, ("get_import_status", {"limit": 1}), ("get_data_status", {}))
    for result, record in ((imports, imports["imports"][0]), (status, status["latest_import"])):
        assert PRIVATE_ONLY_DAY not in json.dumps(result)
        assert all(record[field] is None for field in COVERAGE_FIELDS)
        assert record["status"] == "succeeded" and record["rows_loaded"] == 90  # the audit trail stays


async def test_import_records_keep_the_span_when_nothing_is_private(tmp_path):
    (imports,) = await _call(tmp_path, "", ("get_import_status", {"limit": 1}))
    assert imports["imports"][0]["coverage_after_start"] == PRIVATE_ONLY_DAY
