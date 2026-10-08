"""
Source coverage matrix (source_coverage.py): measured longitudinal coverage per (importer, source, metric),
per metric and per domain, with deterministic green/yellow/red classification. Built on the real schema, so
daily_metrics is the trigger-maintained projection the analytics tools read.
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import source_coverage  # noqa: E402
from db import invariant  # noqa: E402
from logic import connect_writable, ensure_schema  # noqa: E402
from metric_registry import METRICS  # noqa: E402
from source_coverage import compute_source_coverage  # noqa: E402

TODAY = date(2026, 10, 3)


def _conn(tmp_path):
    conn = connect_writable(tmp_path / "health.db")
    ensure_schema(conn)
    return conn


def _add(conn, metric, offsets, *, value=1.0, source=None, importer=None, unit=None, source_type=None, hour=12):
    """One measurement per offset (days before TODAY)."""
    for offset in offsets:
        day = TODAY - timedelta(days=offset)
        conn.execute(
            "INSERT INTO measurements (timestamp, metric, value, unit, source, source_type, importer, imported_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"{day.isoformat()}T{hour:02d}:00:00",
                metric,
                value,
                unit,
                source,
                source_type,
                importer,
                "2026-10-01T08:00:00" if importer else None,
            ),
        )
    conn.commit()


def _import(conn, importer, status="succeeded"):
    conn.execute(
        "INSERT INTO imports (importer, status, started_at) VALUES (?, ?, '2026-10-01T08:00:00')", (importer, status)
    )
    conn.commit()


def _report(conn, **kwargs):
    return compute_source_coverage(conn, TODAY, **kwargs)


def _row(report, metric, importer=None, source=None):
    (match,) = [r for r in report["rows"] if (r["metric"], r["importer"], r["source"]) == (metric, importer, source)]
    return match


def _metric(report, metric):
    (match,) = [m for m in report["metrics"] if m["metric"] == metric]
    return match


def _domain(report, name):
    (match,) = [d for d in report["domains"] if d["domain"] == name]
    return match


def test_empty_database_reports_every_metric_and_domain_as_missing(tmp_path):
    report = _report(_conn(tmp_path))
    assert report["rows"] == []
    assert [m["metric"] for m in report["metrics"]] == list(METRICS)
    for entry in report["metrics"]:
        assert (entry["classification"], entry["limitations"], entry["coverage_days"]) == ("red", ["no_data"], 0)
        assert entry["available_from"] is None and entry["freshness"] is None
    assert all(d["classification"] == "red" for d in report["domains"])
    assert {d["domain"] for d in report["domains"] if not d["modelled"]} == {"readiness", "training", "routes"}
    assert _domain(report, "workouts")["limitations"] == ["no_data"]


def test_complete_recent_history_from_a_healthy_import_is_green(tmp_path):
    conn = _conn(tmp_path)
    _add(conn, "steps", range(0, 60), importer="csv")
    _import(conn, "csv")
    report = _report(conn)
    row = _row(report, "steps", importer="csv")
    assert row["available_from"] == (TODAY - timedelta(days=59)).isoformat()
    assert row["latest_available"] == TODAY.isoformat()
    assert (row["coverage_days"], row["span_days"], row["gap_count"], row["coverage_ratio"]) == (60, 60, 0, 1.0)
    assert (row["days_behind"], row["freshness"], row["import_status"]) == (0, "CURRENT", "succeeded")
    assert (row["classification"], row["limitations"], row["analytics_supported"]) == ("green", [], True)
    assert _metric(report, "steps")["classification"] == "green"
    assert _domain(report, "activity")["classification"] == "red"  # workout_minutes has no data
    assert report["summary"] == {"red": 0, "yellow": 0, "green": 1}


def test_gaps_decide_yellow_then_red(tmp_path):
    conn = _conn(tmp_path)
    _add(conn, "steps", [o for o in range(60) if o % 5 != 1], importer="csv")  # 48 of 60 days: ratio 0.8
    _add(conn, "sleep_hours", [o for o in range(60) if o % 3 == 0], importer="csv")  # 20 of 58 days
    _import(conn, "csv")
    report = _report(conn)
    steps = _row(report, "steps", importer="csv")
    assert steps["gap_count"] == 12 and steps["missing_days"] == 12
    assert (steps["classification"], steps["limitations"]) == ("yellow", ["gaps_in_coverage"])
    sleep = _row(report, "sleep_hours", importer="csv")
    assert (sleep["classification"], sleep["limitations"]) == ("red", ["incomplete_coverage"])


def test_stale_and_short_history_are_yellow(tmp_path):
    conn = _conn(tmp_path)
    _add(conn, "steps", range(10, 70), importer="csv")
    _add(conn, "water_ml", range(0, 10), importer="csv")
    _import(conn, "csv")
    report = _report(conn)
    stale = _row(report, "steps", importer="csv")
    assert (stale["freshness"], stale["days_behind"]) == ("STALE", 10)
    assert (stale["classification"], stale["limitations"]) == ("yellow", ["stale"])
    short = _row(report, "water_ml", importer="csv")
    assert (short["classification"], short["limitations"]) == ("yellow", ["short_history"])


def test_stale_after_days_is_respected(tmp_path):
    conn = _conn(tmp_path)
    _add(conn, "steps", range(10, 70), importer="csv")
    _import(conn, "csv")
    row = _row(_report(conn, stale_after_days=10), "steps", importer="csv")
    assert (row["freshness"], row["classification"]) == ("CURRENT", "green")


def test_failed_and_interrupted_imports_make_their_sources_red(tmp_path):
    conn = _conn(tmp_path)
    _add(conn, "steps", range(60), importer="csv")
    _add(conn, "sleep_hours", range(60), importer="apple-health", source="Apple Watch")
    _add(conn, "mood", range(60), importer="health-connect")
    _import(conn, "csv", "succeeded")
    _import(conn, "csv", "failed")
    _import(conn, "apple-health", "running")
    report = _report(conn)
    assert _row(report, "steps", "csv")["limitations"] == ["latest_import_failed"]
    assert _row(report, "sleep_hours", "apple-health", "Apple Watch")["limitations"] == ["latest_import_interrupted"]
    assert _row(report, "steps", "csv")["classification"] == "red"
    assert _row(report, "mood", "health-connect")["import_status"] == "no_import_record"
    assert _row(report, "mood", "health-connect")["classification"] == "green"
    assert _metric(report, "steps")["import_status"] == "failed"
    assert _metric(report, "steps")["classification"] == "red"


def test_manual_logs_have_no_import_but_are_still_classified(tmp_path):
    conn = _conn(tmp_path)
    _add(conn, "mood", range(40), source="daily-log")
    row = _row(_report(conn), "mood", source="daily-log")
    assert (row["importer"], row["import_status"], row["classification"]) == (None, "manual", "green")


def test_episodic_metrics_are_not_penalised_for_expected_gaps(tmp_path):
    conn = _conn(tmp_path)
    _add(conn, "weight_kg", range(3, 70, 7), importer="csv")  # weekly weighing, latest 3 days ago
    _import(conn, "csv")
    report = _report(conn)
    row = _row(report, "weight_kg", importer="csv")
    assert row["coverage_ratio"] < source_coverage.GREEN_MIN_COVERAGE_RATIO and row["gap_count"] > 0
    assert (row["freshness"], row["classification"], row["limitations"]) == ("CURRENT", "green", [])
    assert _metric(report, "weight_kg")["cadence"] == "episodic"
    assert _domain(report, "body_measurements")["classification"] == "green"


def test_episodic_metric_goes_stale_after_its_own_threshold(tmp_path):
    conn = _conn(tmp_path)
    _add(conn, "weight_kg", [40, 47, 54, 61, 68], importer="csv")
    _import(conn, "csv")
    row = _row(_report(conn), "weight_kg", importer="csv")
    assert (row["freshness"], row["days_behind"], row["limitations"]) == ("STALE", 40, ["stale"])


def test_mixed_hrv_statistics_are_flagged_on_rows_and_metric(tmp_path):
    conn = _conn(tmp_path)
    _add(conn, "hrv_ms", range(0, 60, 2), importer="apple-health", source="Apple Watch", value=40.0)
    _add(conn, "hrv_ms", range(1, 60, 2), importer="health-connect", value=55.0)
    _import(conn, "apple-health")
    _import(conn, "health-connect")
    report = _report(conn)
    apple = _row(report, "hrv_ms", "apple-health", "Apple Watch")
    connect = _row(report, "hrv_ms", "health-connect")
    assert apple["provenance"]["statistic"] == "sdnn" and connect["provenance"]["statistic"] == "rmssd"
    for row in (apple, connect):
        assert "mixed_statistics" in row["limitations"] and row["classification"] != "green"
    metric = _metric(report, "hrv_ms")
    assert metric["statistics"] == ["rmssd", "sdnn"] and "mixed_statistics" in metric["limitations"]
    assert _domain(report, "hrv")["classification"] != "green"


def test_single_statistic_is_not_flagged(tmp_path):
    conn = _conn(tmp_path)
    _add(conn, "hrv_ms", range(60), importer="apple-health", source="Apple Watch", value=40.0)
    _import(conn, "apple-health")
    metric = _metric(_report(conn), "hrv_ms")
    assert (metric["statistics"], metric["classification"]) == (["sdnn"], "green")


def test_provenance_reports_units_types_counts_and_import_time(tmp_path):
    conn = _conn(tmp_path)
    _add(conn, "heart_rate", range(30), importer="apple-health", source="Apple Watch", unit="count/min", hour=8)
    _add(conn, "heart_rate", range(30), importer="apple-health", source="Apple Watch", unit="count/min", hour=9)
    _add(conn, "heart_rate", range(30), importer="apple-health", source="Apple Watch", unit="bpm", source_type="watch")
    _import(conn, "apple-health")
    prov = _row(_report(conn), "heart_rate", "apple-health", "Apple Watch")["provenance"]
    assert prov["measurement_count"] == 90
    assert prov["units"] == ["bpm", "count/min"] and prov["source_types"] == ["watch"]
    assert prov["last_imported_at"] == "2026-10-01T08:00:00"


def test_resolution_is_reported_and_unranked_conflicts_are_a_limitation(tmp_path):
    conn = _conn(tmp_path)
    _add(conn, "steps", range(40), source="Apple Watch", importer="apple-health", hour=8, value=100.0)
    _add(conn, "steps", range(40), source="Garmin", importer="csv", hour=20, value=900.0)
    _import(conn, "apple-health")
    _import(conn, "csv")
    report = _report(conn)
    steps = _metric(report, "steps")
    assert steps["resolution_days"] == {"single": 0, "priority": 0, "fallback": 40}
    assert steps["multi_source_days"] == 40 and "unranked_source_resolution" in steps["limitations"]
    assert sum(steps["resolved_sources"].values()) == 40
    wins = {r["source"]: r["provenance"]["days_resolved_to_source"] for r in report["rows"]}
    assert sorted(wins.values()) == [0, 40]

    invariant.set_source_priority(conn, "steps", ["Garmin", "Apple Watch"])
    ranked = _metric(_report(conn), "steps")
    assert ranked["resolution_days"] == {"single": 0, "priority": 40, "fallback": 0}
    assert ranked["resolved_sources"] == {"Garmin": 40}
    assert "unranked_source_resolution" not in ranked["limitations"]


def test_private_metrics_are_left_out_of_every_level(tmp_path):
    conn = _conn(tmp_path)
    _add(conn, "hrv_ms", range(60), importer="apple-health", source="Apple Watch")
    _add(conn, "steps", range(60), importer="csv")
    _add(conn, "water_ml", range(60), importer="csv")
    _import(conn, "csv")
    report = _report(conn, exclude_metrics=frozenset({"hrv_ms", "workout_minutes"}))
    assert "hrv_ms" not in json.dumps(report)
    assert "workout_minutes" not in {m["metric"] for m in report["metrics"]}
    assert {d["domain"] for d in report["domains"]} >= {"sleep", "heart_rate", "activity", "body_measurements"}
    assert "hrv" not in {d["domain"] for d in report["domains"]}
    assert "workouts" not in {d["domain"] for d in report["domains"]}
    assert [m["metric"] for m in report["metrics"] if m["metric"] == "steps"] == ["steps"]
    activity = _domain(report, "activity")["metrics"]
    assert activity == [{"metric": "steps", "classification": "green", "coverage_days": 60}]


def test_workouts_domain_comes_from_workout_sessions(tmp_path):
    conn = _conn(tmp_path)
    for offset in (0, 2, 4, 4, 9, 20, 31):
        conn.execute(
            "INSERT INTO workout_sessions (date, activity_type, duration_minutes) VALUES (?, 'run', 30)",
            ((TODAY - timedelta(days=offset)).isoformat(),),
        )
    conn.commit()
    workouts = _domain(_report(conn), "workouts")
    assert workouts["source_table"] == "workout_sessions" and workouts["session_count"] == 7
    assert (workouts["coverage_days"], workouts["available_from"]) == (6, (TODAY - timedelta(days=31)).isoformat())
    assert (workouts["freshness"], workouts["classification"], workouts["limitations"]) == ("CURRENT", "green", [])


def test_unmodelled_domains_are_explicitly_red(tmp_path):
    report = _report(_conn(tmp_path))
    for name in ("readiness", "training", "routes"):
        domain = _domain(report, name)
        assert (domain["modelled"], domain["classification"], domain["limitations"]) == (False, "red", ["not_modelled"])
        assert domain["note"]


def test_domain_is_as_strong_as_its_weakest_metric(tmp_path):
    conn = _conn(tmp_path)
    _add(conn, "heart_rate", range(60), importer="csv")
    _import(conn, "csv")
    heart = _domain(_report(conn), "heart_rate")
    assert heart["classification"] == "red"  # resting_heart_rate has no data
    assert {m["metric"]: m["classification"] for m in heart["metrics"]} == {
        "heart_rate": "green",
        "resting_heart_rate": "red",
    }


def test_report_is_read_only_and_deterministic(tmp_path):
    conn = _conn(tmp_path)
    _add(conn, "steps", range(45), importer="csv", source="Garmin")
    _add(conn, "hrv_ms", range(45), importer="apple-health", source="Apple Watch")
    _import(conn, "csv")
    before = conn.total_changes
    first = _report(conn)
    assert _report(conn) == first
    assert conn.total_changes == before
    assert json.loads(json.dumps(first)) == first


def test_cli_prints_json_and_text_and_reports_a_missing_database(tmp_path, capsys):
    conn = _conn(tmp_path)
    _add(conn, "steps", range(60), importer="csv")
    _import(conn, "csv")
    conn.close()
    db = tmp_path / "health.db"
    assert source_coverage.main(["--db", str(db), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["metrics"][0]["metric"] == "steps"
    assert source_coverage.main(["--db", str(db)]) == 0
    text = capsys.readouterr().out
    assert "Domains" in text and "readiness" in text and "steps" in text
    assert source_coverage.main(["--db", str(tmp_path / "missing.db")]) == 1


def test_database_without_an_imports_table_reports_no_import_record(tmp_path):
    conn = _conn(tmp_path)
    _add(conn, "steps", range(60), importer="csv")
    conn.execute("DROP TABLE imports")
    conn.commit()
    report = _report(conn)
    row = _row(report, "steps", importer="csv")
    assert (row["import_status"], row["classification"]) == ("no_import_record", "green")


def test_cli_reports_an_unreadable_schema_instead_of_a_traceback(tmp_path, capsys):
    import sqlite3

    db = tmp_path / "old.db"
    bare = sqlite3.connect(db)
    bare.execute("CREATE TABLE health (date TEXT PRIMARY KEY, steps INTEGER)")
    bare.commit()
    bare.close()
    assert source_coverage.main(["--db", str(db)]) == 1
    err = capsys.readouterr().err
    assert "Could not read" in err and "never modifies the database" in err
