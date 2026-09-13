import sqlite3
from pathlib import Path

import doctor


def _make_db(tmp_path: Path, with_data: bool) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "health.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE health (date TEXT PRIMARY KEY, steps INTEGER, "
        "sleep_hours REAL, resting_heart_rate INTEGER, weight_kg REAL, "
        "workout_minutes INTEGER, mood TEXT, water_ml INTEGER)"
    )
    if with_data:
        conn.execute(
            "INSERT INTO health (date, steps, sleep_hours, resting_heart_rate) "
            "VALUES ('2026-01-01', 8000, 7.5, 60)"
        )
    conn.commit()
    conn.close()
    return db_path


def test_check_python_current_interpreter():
    assert doctor.check_python() is True


def test_check_database_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(doctor, "HEALTH_DB", tmp_path / "data" / "health.db")
    assert doctor.check_database() is None


def test_check_days_and_metrics(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, with_data=True)
    monkeypatch.setattr(doctor, "HEALTH_DB", db_path)
    conn = sqlite3.connect(db_path)
    assert doctor.check_days(conn) == 1
    assert doctor.check_metrics(conn) >= 3
    conn.close()


def test_check_days_empty_db(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path, with_data=False)
    conn = sqlite3.connect(db_path)
    assert doctor.check_days(conn) == 0
    conn.close()


def test_main_runs_end_to_end(tmp_path, monkeypatch, capsys):
    _make_db(tmp_path, with_data=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(doctor, "HEALTH_DB", tmp_path / "data" / "health.db")
    assert doctor.main() == 0
    out = capsys.readouterr().out
    assert "Next step:" in out
