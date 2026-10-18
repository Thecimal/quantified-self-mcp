import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import doctor
from init_db import init_health_db
from logic import connect_writable, ensure_schema

SAMPLE_CSV = Path(__file__).resolve().parent.parent / "sample_data" / "health_sample.csv"


def _make_db(tmp_path: Path, with_data: bool) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "health.db"
    # Built with the real importer / schema so this can't drift from what the tools actually write.
    if with_data:
        init_health_db(SAMPLE_CSV, db_path, replace=True)
    else:
        conn = connect_writable(db_path)
        ensure_schema(conn)
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
    assert doctor.check_days(conn) == 30
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
    assert "Database contains 30 days" in out
    assert "\u2717" not in out


def test_health_db_follows_health_db_path(tmp_path):
    # The server and importer honour HEALTH_DB_PATH; the doctor must look at the same file,
    # from any working directory, not at a relative ./data/health.db.
    target = tmp_path / "elsewhere" / "h.db"
    env = {**os.environ, "HEALTH_DB_PATH": str(target)}
    repo = Path(__file__).resolve().parent.parent
    out = subprocess.run(
        [sys.executable, "-c", "import doctor; print(doctor.HEALTH_DB)"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert Path(out) == target
