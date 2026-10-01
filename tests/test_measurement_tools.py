"""
Targeted tests for tools/measurements.py (the four raw-measurement tools).

Written from an audit of the branches the coverage report showed as
unexecuted (log_measurement's validation/DB-error paths, all of
read_measurements, aggregate_measurements' error and empty-day paths, and
the whole body of get_metric_provenance). Each test pins down observable
behavior -- stored data, returned values, error codes -- rather than just
executing lines.

Two tests are strict xfails. They assert the behavior the docs and the
sibling tools promise, and they currently fail because of real defects found
during the audit (end_date excluding same-day timestamped rows, no value
validation). strict=True means the suite goes red the moment a fix lands,
forcing the marker to be removed deliberately. (The private-field leakage
xfail was promoted to the HEALTH_PRIVATE_FIELDS tests below once fixed.)
"""

import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _fresh_server(tmp_path, monkeypatch, private_fields=""):
    monkeypatch.setenv("HEALTH_DB_PATH", str(tmp_path / "health.db"))
    monkeypatch.setenv("HEALTH_PRIVATE_FIELDS", private_fields)
    for name in ("server", "privacy", "tools.health", "tools.measurements", "tools.workouts"):
        sys.modules.pop(name, None)
    import server

    return server


@pytest.fixture
def srv(tmp_path, monkeypatch):
    return _fresh_server(tmp_path, monkeypatch)


@pytest.fixture
def private_srv(tmp_path, monkeypatch):
    return _fresh_server(tmp_path, monkeypatch, private_fields="weight_kg")


class _Log:
    def __init__(self):
        self.errors = []

    def error(self, msg, *args):
        self.errors.append(msg % args if args else msg)


def _tools_with(srv, *, readonly_connection=None):
    """Register the measurement tools on a throwaway FastMCP, optionally
    swapping the read-only connection factory so DB failures can be injected
    into the closure-bound dependency (not reachable by monkeypatching)."""
    import tools.measurements as tm

    log = _Log()
    funcs = tm.register_measurement_tools(
        FastMCP("t"),
        db_path=srv.HEALTH_DB_PATH,
        logger=log,
        readonly_connection=readonly_connection or srv._readonly_connection,
        metric_columns=srv.METRIC_COLUMNS,
    )
    return (*funcs, log)


@contextmanager
def _failing_readonly(message):
    raise sqlite3.OperationalError(message)
    yield  # pragma: no cover


# --------------------------------------------------------------------------
# log_measurement
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["not-a-date", "2026-13-40", "", "20260110"])
def test_log_measurement_rejects_bad_timestamp_and_stores_nothing(srv, bad):
    with pytest.raises(ToolError, match=r"\[invalid_timestamp\]"):
        srv.log_measurement(timestamp=bad, metric="steps", value=1)
    assert srv.read_measurements().count == 0


@pytest.mark.parametrize("bad", ["2026-03-01T25:99:00", "2026-03-01Xjunk", "2026-03-01 garbage"])
def test_log_measurement_rejects_timestamp_with_an_unparseable_time_part(srv, bad):
    """P0.2 regression: only timestamp[:10] used to be validated. A valid date followed by junk was stored,
    but SQLite's date() is NULL for it, so the row never reached daily_metrics (verify() == mismatch) while
    the tool reported success."""
    with pytest.raises(ToolError, match=r"\[invalid_timestamp\]"):
        srv.log_measurement(timestamp=bad, metric="steps", value=1)
    assert srv.read_measurements().count == 0


@pytest.mark.parametrize("blank", ["", "   "])
def test_log_measurement_rejects_blank_metric(srv, blank):
    with pytest.raises(ToolError, match=r"\[invalid_metric\].*non-empty"):
        srv.log_measurement(timestamp="2026-01-10T08:00:00", metric=blank, value=1)
    assert srv.read_measurements().count == 0


def test_rejected_unknown_metric_leaves_no_measurement_or_projection_row(srv):
    """The trigger aborts the INSERT; nothing may be half-written."""
    with pytest.raises(ToolError, match=r"\[invalid_metric\]"):
        srv.log_measurement(timestamp="2026-01-10T08:00:00", metric="bogus", value=1)
    assert srv.read_measurements().count == 0
    assert srv.read_health_data(start_date="2026-01-10", end_date="2026-01-10").rows == []


def test_unknown_metric_error_lists_supported_metrics(srv):
    with pytest.raises(ToolError) as exc:
        srv.log_measurement(timestamp="2026-01-10T08:00:00", metric="bogus", value=1)
    assert "Supported metrics:" in str(exc.value)
    assert "resting_heart_rate" in str(exc.value)


def test_unknown_metric_error_survives_failed_supported_metric_lookup(srv):
    """If listing the supported metrics itself fails, the caller must still
    get the real cause (invalid_metric), not a misleading database error."""
    log_measurement, *_ = _tools_with(srv, readonly_connection=lambda p: _failing_readonly("disk I/O error"))
    with pytest.raises(ToolError) as exc:
        log_measurement(timestamp="2026-01-10T08:00:00", metric="bogus", value=1)
    assert "[invalid_metric]" in str(exc.value)
    assert "Supported metrics" not in str(exc.value)


def test_log_measurement_locked_database_reports_locked_and_does_not_leak_path(srv, monkeypatch):
    import tools.measurements as tm

    def _locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    log_measurement, *_rest, log = _tools_with(srv)
    monkeypatch.setattr(tm, "connect_writable", _locked)
    with pytest.raises(ToolError) as exc:
        log_measurement(timestamp="2026-01-10T08:00:00", metric="steps", value=1)
    assert "[database_locked]" in str(exc.value)
    assert str(srv.HEALTH_DB_PATH) not in str(exc.value)
    assert log.errors, "the underlying failure must be logged server-side"


def test_log_measurement_non_lock_db_failure_reports_database_error(srv, monkeypatch):
    import tools.measurements as tm

    def _broken(*a, **k):
        raise sqlite3.OperationalError("unable to open database file")

    log_measurement, *_ = _tools_with(srv)
    monkeypatch.setattr(tm, "connect_writable", _broken)
    with pytest.raises(ToolError, match=r"\[database_error\]"):
        log_measurement(timestamp="2026-01-10T08:00:00", metric="steps", value=1)


def test_log_measurement_returns_exactly_what_was_stored(srv):
    out = srv.log_measurement(
        timestamp="2026-01-10T07:30:00",
        metric="resting_heart_rate",
        value=61,
        unit="bpm",
        source="Apple Watch",
        source_type="wearable",
    ).measurement
    stored = srv.read_measurements(metric="resting_heart_rate").measurements
    assert len(stored) == 1
    assert stored[0] == out
    assert (out.value, out.unit, out.source, out.source_type) == (61.0, "bpm", "Apple Watch", "wearable")


# --------------------------------------------------------------------------
# read_measurements (previously never executed by any test)
# --------------------------------------------------------------------------


@pytest.fixture
def seeded(srv):
    L = srv.log_measurement
    L(timestamp="2026-01-10T08:00:00", metric="steps", value=100, source="Phone")
    L(timestamp="2026-01-11T09:00:00", metric="steps", value=200, source="Watch")
    L(timestamp="2026-01-12T10:00:00", metric="steps", value=300, source="Phone")
    L(timestamp="2026-01-11T07:00:00", metric="resting_heart_rate", value=60, source="Watch")
    return srv


def test_read_measurements_empty_database(srv):
    result = srv.read_measurements()
    assert result.measurements == []
    assert result.count == 0


def test_read_measurements_most_recent_first_with_matching_count(seeded):
    result = seeded.read_measurements()
    stamps = [m.timestamp for m in result.measurements]
    assert stamps == sorted(stamps, reverse=True)
    assert result.count == len(result.measurements) == 4


def test_read_measurements_filters_by_metric_and_source(seeded):
    assert [m.value for m in seeded.read_measurements(metric="steps").measurements] == [300, 200, 100]
    assert [m.value for m in seeded.read_measurements(source="Watch").measurements] == [200, 60]
    both = seeded.read_measurements(metric="steps", source="Watch")
    assert [m.value for m in both.measurements] == [200]


def test_read_measurements_start_date_is_inclusive_of_the_whole_day(seeded):
    got = [m.value for m in seeded.read_measurements(metric="steps", start_date="2026-01-11").measurements]
    assert got == [300, 200]


def test_read_measurements_limit_keeps_the_most_recent_rows(seeded):
    result = seeded.read_measurements(metric="steps", limit=2)
    assert [m.value for m in result.measurements] == [300, 200]
    assert result.count == 2


def test_read_measurements_read_failure_reports_database_error(srv, monkeypatch):
    import tools.measurements as tm

    def _broken(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    _, read_measurements, *_rest, log = _tools_with(srv)
    monkeypatch.setattr(tm, "connect_writable", _broken)
    with pytest.raises(ToolError, match=r"\[database_error\]"):
        read_measurements()
    assert log.errors


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT: end_date compares lexicographically against full ISO timestamps, so "
    "'2026-01-10T08:00:00' > '2026-01-10' and rows on end_date itself are dropped, "
    "contradicting the docstring ('on/before this date').",
)
def test_read_measurements_end_date_includes_that_whole_day(seeded):
    got = [m.value for m in seeded.read_measurements(metric="steps", end_date="2026-01-11").measurements]
    assert got == [200, 100]


# --------------------------------------------------------------------------
# aggregate_measurements
# --------------------------------------------------------------------------


def test_aggregate_measurements_rejects_bad_date(srv):
    with pytest.raises(ToolError, match=r"\[invalid_date\]"):
        srv.aggregate_measurements(date="2026-02-30")


def test_aggregate_measurements_day_without_data_is_empty_not_error(srv):
    result = srv.aggregate_measurements(date="2030-01-01")
    assert result.aggregated == {}
    assert result.row.model_dump(exclude_none=True) == {"date": "2030-01-01"}


def test_aggregate_measurements_read_failure_reports_database_error(srv):
    *_, aggregate_measurements, _prov, log = _tools_with(
        srv, readonly_connection=lambda p: _failing_readonly("file is not a database")
    )
    with pytest.raises(ToolError, match=r"\[database_error\].*re-run init_db"):
        aggregate_measurements(date="2026-01-10")
    assert log.errors


# --------------------------------------------------------------------------
# get_metric_provenance (tool body previously never executed)
# --------------------------------------------------------------------------


def test_provenance_rejects_bad_date(srv):
    with pytest.raises(ToolError, match=r"\[invalid_date\]"):
        srv.get_metric_provenance(metric="resting_heart_rate", date="yesterday")


def test_provenance_flags_conflict_between_sources_with_per_source_stats(srv):
    L = srv.log_measurement
    L(timestamp="2026-01-10T07:00:00", metric="resting_heart_rate", value=58, source="Apple Watch")
    L(timestamp="2026-01-10T07:10:00", metric="resting_heart_rate", value=60, source="Apple Watch")
    L(timestamp="2026-01-10T08:00:00", metric="resting_heart_rate", value=70, source="Garmin")
    result = srv.get_metric_provenance(metric="resting_heart_rate", date="2026-01-10")
    by_source = {s.source: s for s in result.sources}
    assert result.conflict is True
    assert by_source["Apple Watch"].value == 59.0 and by_source["Apple Watch"].n == 2
    assert by_source["Garmin"].value == 70.0 and by_source["Garmin"].n == 1


def test_provenance_single_source_is_not_a_conflict(srv):
    srv.log_measurement(timestamp="2026-01-10T07:00:00", metric="steps", value=500, source="Phone")
    result = srv.get_metric_provenance(metric="steps", date="2026-01-10")
    assert result.conflict is False
    assert [s.source for s in result.sources] == ["Phone"]


def test_provenance_no_data_returns_empty_sources(srv):
    result = srv.get_metric_provenance(metric="steps", date="2030-01-01")
    assert result.sources == []
    assert result.conflict is False


def test_provenance_read_failure_reports_database_error(srv, monkeypatch):
    import tools.measurements as tm

    def _broken(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    *_, get_metric_provenance, log = _tools_with(srv)
    monkeypatch.setattr(tm, "connect_writable", _broken)
    with pytest.raises(ToolError, match=r"\[database_error\]"):
        get_metric_provenance(metric="steps", date="2026-01-10")
    assert log.errors


# --------------------------------------------------------------------------
# Known defects found by the audit (strict xfail -- see module docstring)
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# HEALTH_PRIVATE_FIELDS (P0.3 regression). The README promises private fields
# are "excluded from MCP read and analytical operations"; read_health_data,
# log_daily_metric, the export and the analytics tools honored that, but the
# four measurement tools returned the raw value.
# --------------------------------------------------------------------------


def test_private_metric_values_never_leave_the_measurement_tools(private_srv):
    private_srv.log_measurement(timestamp="2026-01-10T07:00:00", metric="weight_kg", value=81.5, source="Scale")
    echoed = private_srv.log_measurement(
        timestamp="2026-01-10T08:00:00", metric="weight_kg", value=82.0, source="Scale"
    ).measurement
    private_srv.log_measurement(timestamp="2026-01-10T09:00:00", metric="steps", value=1234)

    read = private_srv.read_measurements().measurements
    agg = private_srv.aggregate_measurements(date="2026-01-10")

    assert echoed.value is None
    assert [m.value for m in read if m.metric == "weight_kg"] == [None, None]
    assert "weight_kg" not in agg.aggregated
    assert agg.row.weight_kg is None
    # Only private metrics are redacted.
    assert [m.value for m in read if m.metric == "steps"] == [1234]
    assert agg.aggregated["steps"] == 1234


def test_private_metric_is_still_stored_locally(private_srv, tmp_path):
    private_srv.log_measurement(timestamp="2026-01-10T07:00:00", metric="weight_kg", value=81.5, source="Scale")
    with sqlite3.connect(tmp_path / "health.db") as conn:
        assert conn.execute("SELECT value FROM measurements WHERE metric = 'weight_kg'").fetchall() == [(81.5,)]


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.read_measurements(metric="weight_kg"),
        lambda s: s.get_metric_provenance(metric="weight_kg", date="2026-01-10"),
    ],
    ids=["read_measurements", "get_metric_provenance"],
)
def test_metric_specific_measurement_tools_refuse_a_private_metric(private_srv, call):
    """Same rule as get_metric_history/the analytics tools: provenance's `conflict` flag is computed from the
    private values, so redacting the numbers alone would still leak their shape."""
    private_srv.log_measurement(timestamp="2026-01-10T07:00:00", metric="weight_kg", value=81.5, source="A")
    private_srv.log_measurement(timestamp="2026-01-10T08:00:00", metric="weight_kg", value=95.0, source="B")
    with pytest.raises(ToolError, match=r"\[invalid_field\].*private"):
        call(private_srv)


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT: log_measurement does no value validation (inf, negative steps, mood=99 are "
    "stored and flow into daily_metrics/analytics) while log_daily_metric rejects the same "
    "values with invalid_metric_value; NaN surfaces as a misleading 'database may be locked' error.",
)
@pytest.mark.parametrize(
    "metric,value",
    [("steps", float("inf")), ("steps", -5), ("mood", 99), ("steps", float("nan"))],
)
def test_log_measurement_rejects_values_log_daily_metric_would_reject(srv, metric, value):
    with pytest.raises(ToolError, match=r"\[invalid_metric_value\]"):
        srv.log_measurement(timestamp="2026-01-10T08:00:00", metric=metric, value=value)
    assert srv.read_measurements().count == 0
