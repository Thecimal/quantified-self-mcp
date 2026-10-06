"""
Tests for import history (the `imports` table written by init_db.py) and the
freshness classification in logic.compute_data_status, plus the two MCP tools
that expose them.
"""

import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastmcp import Client

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import init_db  # noqa: E402
from init_db import init_health_db  # noqa: E402
from logic import (  # noqa: E402
    compute_data_status,
    connect_writable,
    ensure_schema,
    list_imports,
    record_import_finish,
    record_import_start,
)

TODAY = date(2026, 10, 3)


def _csv(tmp_path, rows, name="health.csv") -> Path:
    path = tmp_path / name
    path.write_text("date,steps\n" + "".join(f"{d},{s}\n" for d, s in rows), encoding="utf-8")
    return path


def _conn(tmp_path):
    conn = connect_writable(tmp_path / "health.db")
    ensure_schema(conn)
    return conn


def _log(conn, day: date, steps: int = 5000) -> None:
    conn.execute(
        "INSERT INTO measurements (timestamp, metric, value, source) VALUES (?, 'steps', ?, 'daily-log')",
        (f"{day.isoformat()}T12:00:00", steps),
    )
    conn.commit()


def test_empty_database_is_no_data(tmp_path):
    conn = _conn(tmp_path)
    status = compute_data_status(conn, TODAY)
    assert status["status"] == "NO_DATA"
    assert status["action"] == "import_source"
    assert status["latest_data"] is None
    assert status["coverage"] == {"start": None, "end": None}
    assert status["latest_import"] is None


def test_recent_contiguous_data_is_current(tmp_path):
    conn = _conn(tmp_path)
    for offset in range(5):
        _log(conn, TODAY - timedelta(days=offset))
    status = compute_data_status(conn, TODAY)
    assert status["status"] == "CURRENT"
    assert status["reason"] is None and status["action"] is None
    assert status["days_behind"] == 0
    assert status["gap_count"] == 0
    assert status["days_with_data"] == 5


def test_old_data_is_stale_with_days_behind(tmp_path):
    conn = _conn(tmp_path)
    _log(conn, TODAY - timedelta(days=19))
    status = compute_data_status(conn, TODAY)
    assert status["status"] == "STALE"
    assert status["days_behind"] == 19
    assert status["action"] == "import_latest_source"
    assert compute_data_status(conn, TODAY, stale_after_days=30)["status"] == "CURRENT"


def test_gap_inside_coverage_is_incomplete(tmp_path):
    conn = _conn(tmp_path)
    for offset in (0, 1, 5, 6):
        _log(conn, TODAY - timedelta(days=offset))
    status = compute_data_status(conn, TODAY)
    assert status["status"] == "INCOMPLETE"
    assert status["gap_count"] == 1
    assert status["missing_days"] == 3
    assert status["gaps"] == [
        {"start": (TODAY - timedelta(days=4)).isoformat(), "end": (TODAY - timedelta(days=2)).isoformat(), "days": 3}
    ]


def test_excluded_metrics_do_not_count_as_data(tmp_path):
    conn = _conn(tmp_path)
    _log(conn, TODAY)
    assert compute_data_status(conn, TODAY, exclude_metrics={"steps"})["status"] == "NO_DATA"


def test_unfinished_or_failed_latest_import_is_import_failed(tmp_path):
    conn = _conn(tmp_path)
    _log(conn, TODAY)
    source = _csv(tmp_path, [("2026-10-03", 1)])
    import_id = record_import_start(conn, "csv", source)
    interrupted = compute_data_status(conn, TODAY)
    assert (interrupted["status"], interrupted["reason"]) == ("IMPORT_FAILED", "import_interrupted")
    record_import_finish(conn, import_id, "failed", error="boom")
    failed = compute_data_status(conn, TODAY)
    assert (failed["status"], failed["reason"]) == ("IMPORT_FAILED", "import_failed")
    record_import_finish(conn, record_import_start(conn, "csv", source), "succeeded")
    assert compute_data_status(conn, TODAY)["status"] == "CURRENT"


def test_record_import_finish_rejects_unknown_status(tmp_path):
    conn = _conn(tmp_path)
    import_id = record_import_start(conn, "csv", _csv(tmp_path, [("2026-10-03", 1)]))
    with pytest.raises(ValueError):
        record_import_finish(conn, import_id, "running")


def test_import_is_recorded_with_name_hash_and_counts(tmp_path):
    source = _csv(tmp_path, [("2026-01-01", 100), ("2026-01-02", 200)])
    db = tmp_path / "health.db"
    init_health_db(source, db, replace=False)
    conn = connect_writable(db)
    (record,) = list_imports(conn)
    assert record["status"] == "succeeded"
    assert record["importer"] == "csv"
    assert record["source_file"] == "health.csv"
    assert len(record["source_sha256"]) == 64
    assert record["rows_loaded"] == 2 and record["rows_skipped"] == 0
    assert record["measurements_written"] == 2
    assert record["finished_at"] is not None and record["error"] is None


def test_reimporting_the_same_file_adds_history_not_duplicates(tmp_path):
    source = _csv(tmp_path, [("2026-01-01", 100), ("2026-01-02", 200)])
    db = tmp_path / "health.db"
    init_health_db(source, db, replace=False)
    init_health_db(source, db, replace=False)
    conn = connect_writable(db)
    assert conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 2
    first, second = reversed(list_imports(conn, limit=5))
    assert first["source_sha256"] == second["source_sha256"]
    assert [first["status"], second["status"]] == ["succeeded", "succeeded"]


def test_failed_load_is_recorded_and_leaves_existing_data_untouched(tmp_path, monkeypatch):
    db = tmp_path / "health.db"
    init_health_db(_csv(tmp_path, [("2026-01-01", 100)], "first.csv"), db, replace=False)

    def explode(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(init_db, "import_measurements", explode)
    with pytest.raises(RuntimeError, match="disk on fire"):
        init_health_db(_csv(tmp_path, [("2026-01-02", 200)], "second.csv"), db, replace=True)

    conn = sqlite3.connect(db)
    assert conn.execute("SELECT date, value FROM daily_metrics").fetchall() == [("2026-01-01", 100.0)]
    status, error = conn.execute("SELECT status, error FROM imports ORDER BY id DESC LIMIT 1").fetchone()
    assert status == "failed"
    assert error == "RuntimeError: disk on fire"


@pytest.fixture
def health_db(tmp_path, monkeypatch):
    monkeypatch.setenv("HEALTH_DB_PATH", str(tmp_path / "health.db"))
    for mod in ("server", "privacy", "tools.health", "tools.measurements", "tools.workouts", "tools.status"):
        sys.modules.pop(mod, None)
    import server

    return server


async def test_get_data_status_tool_reports_no_data_then_current(health_db):
    async with Client(health_db.mcp) as client:
        empty = (await client.call_tool("get_data_status", {})).structured_content
        assert empty["status"] == "NO_DATA"
        await client.call_tool("log_daily_metric", {"date": date.today().isoformat(), "steps": 4000})
        current = (await client.call_tool("get_data_status", {})).structured_content
        assert current["status"] == "CURRENT"
        assert current["latest_data"] == date.today().isoformat()


async def test_status_tools_reject_out_of_range_arguments(health_db):
    async with Client(health_db.mcp) as client:
        for name, args in (("get_data_status", {"stale_after_days": -1}), ("get_import_status", {"limit": 0})):
            result = await client.call_tool(name, args, raise_on_error=False)
            assert result.is_error is True
            assert "[invalid_range]" in result.content[0].text


async def test_get_import_status_tool_lists_runs_newest_first(health_db, tmp_path):
    init_health_db(_csv(tmp_path, [("2026-01-01", 1)], "a.csv"), health_db.HEALTH_DB_PATH, replace=False)
    init_health_db(_csv(tmp_path, [("2026-01-02", 2)], "b.csv"), health_db.HEALTH_DB_PATH, replace=False)
    async with Client(health_db.mcp) as client:
        imports = (await client.call_tool("get_import_status", {"limit": 5})).structured_content["imports"]
    assert [r["source_file"] for r in imports] == ["b.csv", "a.csv"]
    assert all(r["status"] == "succeeded" for r in imports)
