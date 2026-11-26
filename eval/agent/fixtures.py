"""Deterministic fixture datasets for the agent eval.

Every fixture is defined relative to *today* (the server reads the real
clock), so each one promises a data-quality state, not literal dates:
"complete" is 90 consecutive days ending today, "stale" ends 12 days ago, and
so on. tests/test_agent_eval.py checks each promise against the real server.
Values follow fixed formulas, so the right answer to a question is known.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from logic import (  # noqa: E402
    connect_writable,
    dataset_coverage,
    ensure_schema,
    insert_workout_session,
    record_import_finish,
    record_import_start,
)

Row = tuple[str, str, float, str]  # timestamp, metric, value, source

WORKOUTS = [
    (1, "running", 40, "moderate"),
    (3, "cycling", 60, "low"),
    (5, "strength", 45, "high"),
    (9, "running", 35, "moderate"),
]

# (avg, max) heart rate per WORKOUTS offset, for fixtures with session_heart_rates. Three-digit values that
# occur in no id, date or count, so a copy of one in a tool result or an answer is unmistakable.
WORKOUT_HEART_RATES = {1: (151, 183), 3: (126, 147), 5: (138, 169), 9: (129, 158)}

PRIVATE_ONLY_OFFSET = 101  # permission_limited: the one day on which only a private metric has data


def _steps(o: int) -> float:
    return float(7000 + (o % 5) * 400)


def _sleep(o: int) -> float:
    return round((6.2 if o < 7 else 7.5) + (o % 3) * 0.1, 1)


def _hrv(o: int) -> float:
    return float(44 + o % 3 if o < 7 else 52 + o % 4)


def _rhr(o: int) -> float:
    return float(58 + o % 3)


def _weight(o: int) -> float:
    return round(78 + o * 0.03, 2)


def _private_weight(o: int) -> float:
    """Moves exactly with sleep_hours (r = 1), so a tool that lets weight_kg into a correlation would surface
    it, and takes values no other metric does."""
    return round(80 + _sleep(o) * 0.37, 3)


def _mood(o: int) -> float:
    return float(5 + o % 3)


def _series(today: date, metric: str, offsets, fn: Callable[[int], float], source: str = "daily-log", hour: int = 12):
    return [(f"{(today - timedelta(days=o)).isoformat()}T{hour:02d}:00:00", metric, fn(o), source) for o in offsets]


def _core(today: date, offsets) -> list[Row]:
    offsets = list(offsets)
    return (
        _series(today, "steps", offsets, _steps)
        + _series(today, "sleep_hours", offsets, _sleep)
        + _series(today, "hrv_ms", offsets, _hrv)
        + _series(today, "resting_heart_rate", offsets, _rhr)
        + _series(today, "weight_kg", offsets, _weight)
    )


def _complete(today: date) -> list[Row]:
    return _core(today, range(90)) + _series(today, "mood", range(10), _mood)


def _incomplete(today: date) -> list[Row]:
    return _core(today, [o for o in range(90) if not (20 <= o <= 34 or o in (5, 6))])


def _sparse(today: date) -> list[Row]:
    offsets = [0, 3, 9, 14, 20]
    return _series(today, "steps", offsets, _steps) + _series(today, "hrv_ms", offsets, _hrv)


def _conflicting(today: date) -> list[Row]:
    return (
        _series(today, "steps", range(30), _steps, source="Apple Watch", hour=8)
        + _series(today, "steps", range(30), lambda o: _steps(o) - 900, source="Garmin", hour=20)
        + _series(today, "resting_heart_rate", range(30), _rhr, source="Apple Watch", hour=8)
    )


def _permission_limited(today: date) -> list[Row]:
    """`complete`, plus one weight reading on a day (PRIVATE_ONLY_OFFSET days back) that has no other metric:
    where a tool counts days with data across all metrics, that day shows when the private metric was logged."""
    rows = [row for row in _complete(today) if row[1] != "weight_kg"]
    return rows + _series(today, "weight_kg", [*range(90), PRIVATE_ONLY_OFFSET], _private_weight)


@dataclass(frozen=True)
class Fixture:
    name: str
    description: str
    rows: Callable[[date], list[Row]]
    private_fields: str = ""
    workouts: bool = False
    import_status: str | None = None  # "succeeded" | "failed" | None (no import recorded)
    session_heart_rates: bool = False  # workouts carry WORKOUT_HEART_RATES (needs workouts=True)
    import_coverage: bool = False  # the recorded import carries the dataset span, as init_db.py records it


FIXTURES: dict[str, Fixture] = {
    f.name: f
    for f in (
        Fixture("empty", "no data at all", lambda t: []),
        Fixture(
            "complete",
            "90 consecutive days ending today; sleep and HRV drop over the last 7 days",
            _complete,
            workouts=True,
            import_status="succeeded",
        ),
        Fixture(
            "incomplete",
            "90-day window ending today with a 15-day hole and a 2-day hole",
            _incomplete,
            import_status="succeeded",
        ),
        Fixture("sparse", "five observations of steps and HRV in the last three weeks", _sparse),
        Fixture(
            "stale",
            "90 consecutive days ending 12 days ago",
            lambda t: _core(t, range(12, 102)),
            import_status="succeeded",
        ),
        Fixture("conflicting", "steps and resting heart rate from two sources that disagree", _conflicting),
        Fixture(
            "pagination",
            "420 consecutive days of steps (more than one read_health_data page)",
            lambda t: _series(t, "steps", range(420), _steps),
        ),
        Fixture(
            "permission_limited",
            "complete data with weight_kg and heart_rate configured as private; workout sessions carry heart "
            "rate; one day holds only a private weight reading; the recorded import carries the dataset span",
            _permission_limited,
            private_fields="weight_kg,heart_rate",
            workouts=True,
            import_status="succeeded",
            session_heart_rates=True,
            import_coverage=True,
        ),
        Fixture("import_failed", "complete data whose latest import failed", _complete, import_status="failed"),
    )
}


def build_fixture(fixture: Fixture, db_path: Path, today: date) -> None:
    """Create db_path and fill it with the fixture, through the same schema and triggers the server uses."""
    conn = connect_writable(db_path)
    try:
        ensure_schema(conn)
        conn.executemany(
            "INSERT INTO measurements (timestamp, metric, value, source) VALUES (?, ?, ?, ?)", fixture.rows(today)
        )
        conn.commit()
        if fixture.workouts:
            for offset, activity, minutes, intensity in WORKOUTS:
                avg_hr, max_hr = WORKOUT_HEART_RATES[offset] if fixture.session_heart_rates else (None, None)
                insert_workout_session(
                    conn,
                    (today - timedelta(days=offset)).isoformat(),
                    activity,
                    minutes,
                    intensity=intensity,
                    avg_heart_rate=avg_hr,
                    max_heart_rate=max_hr,
                    source="daily-log",
                )
        if fixture.import_status:
            span = dataset_coverage(conn) if fixture.import_coverage else None
            import_id = record_import_start(conn, "csv", Path("health.csv"), coverage_before=span)
            if fixture.import_status == "succeeded":
                record_import_finish(
                    conn,
                    import_id,
                    "succeeded",
                    rows_loaded=90,
                    rows_skipped=0,
                    measurements_written=450,
                    coverage_after=span,
                )
            else:
                record_import_finish(conn, import_id, "failed", error="RuntimeError: disk on fire")
    finally:
        conn.close()
