"""
An import that fails while reading the source file must be visible to the diagnostic tools.

Only failures after parsing used to be recorded in `imports`, so a truncated or unreadable export left no trace:
get_data_status kept answering CURRENT and get_import_status listed only the last good run. These tests drive the
real importer and the real server, check what a client is told, and check that the recorded reason never carries a
record value or the file's full path.
"""

import datetime as dt
import json
import sqlite3
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from fastmcp import Client

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from eval.agent.runner import loaded_server  # noqa: E402
from init_db import _failure_reason, init_health_db  # noqa: E402

TODAY = dt.date.today()


def _record(value, when):
    return (
        '<Record type="HKQuantityTypeIdentifierStepCount" sourceName="Watch" unit="count" '
        f'startDate="{when} +0000" endDate="{when} +0000" value="{value}"/>'
    )


def _good_export(tmp_path):
    days = [TODAY - dt.timedelta(days=offset) for offset in range(3, 0, -1)]
    body = "".join(_record(1000 + i, f"{day} 08:00:00") for i, day in enumerate(days))
    path = tmp_path / "good.xml"
    path.write_text(f'<?xml version="1.0"?><HealthData>{body}</HealthData>')
    return path


def _truncated_export(directory):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "new_export.xml"
    path.write_text('<?xml version="1.0"?><HealthData>' + _record(999999, f"{TODAY} 08:00:00") + '<Record type="HK')
    return path


def _imports(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT id, importer, source_file, status, error FROM imports ORDER BY id").fetchall()
    finally:
        conn.close()


async def _call(client, name, arguments=None):
    return (await client.call_tool(name, arguments or {})).structured_content


async def test_a_truncated_export_turns_data_status_from_current_to_import_failed(tmp_path):
    db = tmp_path / "health.db"
    init_health_db(_good_export(tmp_path), db, replace=False)
    with loaded_server(db) as server:
        async with Client(server.mcp) as client:
            assert (await _call(client, "get_data_status"))["status"] == "CURRENT"

            with pytest.raises(ET.ParseError):  # the importer still raises exactly as before
                init_health_db(_truncated_export(tmp_path / "secret-patient-folder"), db, replace=False)

            status = await _call(client, "get_data_status")
            assert (status["status"], status["reason"], status["action"]) == (
                "IMPORT_FAILED",
                "import_failed",
                "rerun_import",
            )
            latest = (await _call(client, "get_import_status"))["imports"][0]
    assert (latest["status"], latest["importer"], latest["source_file"]) == (
        "failed",
        "apple-health",
        "new_export.xml",
    )
    assert latest["error"].startswith("ParseError: unclosed token: line 1, column")
    assert "secret-patient-folder" not in json.dumps(latest) and "999999" not in json.dumps(latest)
    assert [row[3] for row in _imports(db)] == ["succeeded", "failed"]


def test_a_failed_read_on_a_fresh_database_is_recorded_too(tmp_path):
    db = tmp_path / "health.db"
    with pytest.raises(ET.ParseError):
        init_health_db(_truncated_export(tmp_path), db, replace=False)
    assert [row[3] for row in _imports(db)] == ["failed"]


def test_a_file_the_importer_rejects_with_its_own_message_is_recorded_as_rejected(tmp_path):
    source = tmp_path / "secret-patient-folder" / "records.json"
    source.parent.mkdir()
    source.write_text("{not json")
    db = tmp_path / "health.db"
    with pytest.raises(SystemExit, match="couldn't read"):  # the adapter's own message is unchanged
        init_health_db(source, db, replace=False, source="health-connect")
    (_, importer, source_file, status, error) = _imports(db)[0]
    assert (importer, source_file, status) == ("health-connect", "records.json", "failed")
    assert error == "the importer rejected the source file; its message was printed where the import was run"
    assert "secret-patient-folder" not in error


def test_a_csv_that_is_not_utf8_is_recorded_as_an_encoding_failure(tmp_path):
    source = tmp_path / "health.csv"
    source.write_bytes(b"date,steps\n2026-01-01,5\xe9\n")
    db = tmp_path / "health.db"
    with pytest.raises(UnicodeDecodeError):
        init_health_db(source, db, replace=False)
    assert _imports(db)[0][3:] == (
        "failed",
        "UnicodeDecodeError: 'utf-8' codec can't decode byte 0xe9 in position 23: invalid continuation byte",
    )


def test_a_csv_usage_error_still_exits_with_its_message_and_is_now_recorded(tmp_path):
    source = tmp_path / "health.csv"
    source.write_text("steps\n5\n")  # no date column
    db = tmp_path / "health.db"
    with pytest.raises(SystemExit, match="missing required column: date"):
        init_health_db(source, db, replace=False)
    (_, importer, source_file, status, error) = _imports(db)[0]
    assert (importer, source_file, status) == ("csv", "health.csv", "failed")
    assert "date" not in error and str(tmp_path) not in error


def test_a_successful_import_records_exactly_one_run(tmp_path):
    db = tmp_path / "health.db"
    init_health_db(_good_export(tmp_path), db, replace=False)
    assert [row[3] for row in _imports(db)] == ["succeeded"]


def test_recording_is_diagnostic_only_and_never_hides_the_original_error(tmp_path, capsys):
    unwritable = tmp_path / "no_such_dir" / "health.db"  # the failure cannot be recorded here
    with pytest.raises(ET.ParseError):
        init_health_db(_truncated_export(tmp_path), unwritable, replace=False)
    captured = capsys.readouterr()
    assert captured.out == ""  # nothing is written to stdout, which an MCP stdio client reads
    assert "could not record the failed import" in captured.err


def test_the_recorded_reason_never_carries_a_record_value_or_a_path():
    path = Path("/home/jane/Health Export/export.xml")
    assert _failure_reason(ET.ParseError(f"mismatched tag: line 4, column 9 in {path}"), path) == (
        "ParseError: mismatched tag: line 4, column 9 in export.xml"
    )
    assert _failure_reason(PermissionError(13, "Permission denied", str(path)), path) == (
        "PermissionError: Permission denied"
    )
    assert _failure_reason(OSError(), path) == "OSError: I/O error"
    assert _failure_reason(json.JSONDecodeError("Expecting value", "x", 0), path) == (
        "JSONDecodeError: Expecting value: line 1 column 1 (char 0)"
    )
    unknown = " while reading the source file; re-run the importer in a terminal for the details"
    for exc in (ValueError("invalid literal for int() with base 10: '98.6'"), KeyError("jane.doe@example.com")):
        reason = _failure_reason(exc, path)
        assert reason == type(exc).__name__ + unknown
        assert "98.6" not in reason and "jane" not in reason
