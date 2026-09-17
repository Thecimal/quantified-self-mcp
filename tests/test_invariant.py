import sqlite3

import pytest

from db import invariant


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("PRAGMA foreign_keys = ON")
    invariant.bootstrap(c)
    return c


def insert(conn, metric, value, timestamp):
    conn.execute(
        "INSERT INTO measurements (metric, value, timestamp) VALUES (?, ?, ?)",
        (metric, value, timestamp),
    )
    conn.commit()


def daily(conn, date, metric):
    row = conn.execute(
        "SELECT value, raw_measurement_count FROM daily_metrics WHERE date = ? AND metric = ?",
        (date, metric),
    ).fetchone()
    return row


def test_insert_creates_projection(conn):
    insert(conn, "steps", 1000, "2026-09-17T08:00:00")
    assert daily(conn, "2026-09-17", "steps") == (1000, 1)


def test_insert_same_day_sums(conn):
    insert(conn, "steps", 1000, "2026-09-17T08:00:00")
    insert(conn, "steps", 500, "2026-09-17T20:00:00")
    assert daily(conn, "2026-09-17", "steps") == (1500, 2)


def test_insert_other_day_separate_row(conn):
    insert(conn, "steps", 1000, "2026-09-17T08:00:00")
    insert(conn, "steps", 200, "2026-09-18T08:00:00")
    assert daily(conn, "2026-09-17", "steps") == (1000, 1)
    assert daily(conn, "2026-09-18", "steps") == (200, 1)


def test_mean_metric(conn):
    insert(conn, "hrv_ms", 40, "2026-09-17T08:00:00")
    insert(conn, "hrv_ms", 60, "2026-09-17T20:00:00")
    assert daily(conn, "2026-09-17", "hrv_ms") == (50, 2)


def test_last_metric(conn):
    insert(conn, "weight_kg", 80, "2026-09-17T08:00:00")
    insert(conn, "weight_kg", 79.5, "2026-09-17T20:00:00")
    assert daily(conn, "2026-09-17", "weight_kg") == (79.5, 2)


def test_update_value(conn):
    insert(conn, "steps", 1000, "2026-09-17T08:00:00")
    conn.execute("UPDATE measurements SET value = 1200 WHERE metric = 'steps'")
    conn.commit()
    assert daily(conn, "2026-09-17", "steps") == (1200, 1)


def test_update_moves_date(conn):
    insert(conn, "steps", 1000, "2026-09-17T08:00:00")
    conn.execute("UPDATE measurements SET timestamp = '2026-09-18T08:00:00' WHERE metric = 'steps'")
    conn.commit()
    assert daily(conn, "2026-09-17", "steps") is None  # old row cleaned up
    assert daily(conn, "2026-09-18", "steps") == (1000, 1)


def test_update_changes_metric(conn):
    insert(conn, "steps", 1000, "2026-09-17T08:00:00")
    conn.execute("UPDATE measurements SET metric = 'workout_minutes' WHERE metric = 'steps'")
    conn.commit()
    assert daily(conn, "2026-09-17", "steps") is None
    assert daily(conn, "2026-09-17", "workout_minutes") == (1000, 1)


def test_delete(conn):
    insert(conn, "steps", 1000, "2026-09-17T08:00:00")
    insert(conn, "steps", 500, "2026-09-17T20:00:00")
    conn.execute("DELETE FROM measurements WHERE value = 500")
    conn.commit()
    assert daily(conn, "2026-09-17", "steps") == (1000, 1)


def test_delete_last_measurement_removes_daily_row(conn):
    insert(conn, "steps", 1000, "2026-09-17T08:00:00")
    conn.execute("DELETE FROM measurements WHERE metric = 'steps'")
    conn.commit()
    assert daily(conn, "2026-09-17", "steps") is None


def test_missing_rule_rolls_back(conn):
    with pytest.raises(sqlite3.IntegrityError):
        insert(conn, "unknown_metric", 1, "2026-09-17T08:00:00")
    assert conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 0


def test_verify_clean_after_normal_writes(conn):
    insert(conn, "steps", 1000, "2026-09-17T08:00:00")
    insert(conn, "hrv_ms", 45, "2026-09-17T08:00:00")
    assert invariant.verify(conn)["status"] == "ok"


def test_verify_detects_corruption_then_repair_cleans(conn):
    insert(conn, "steps", 1000, "2026-09-17T08:00:00")
    conn.execute("UPDATE daily_metrics SET value = 9999 WHERE metric = 'steps'")
    conn.commit()

    result = invariant.verify(conn)
    assert result["status"] == "mismatch"
    assert any(i["metric"] == "steps" for i in result["issues"])

    repaired = invariant.repair(conn)
    assert repaired["status"] == "ok"


def test_verify_detects_orphaned_daily_row(conn):
    insert(conn, "steps", 1000, "2026-09-17T08:00:00")
    conn.execute("DELETE FROM measurements")
    # daily_metrics still has the row because the DELETE trigger only fires
    # for rows deleted through measurements, which it was -- simulate a true
    # orphan by inserting one directly instead.
    conn.execute(
        "INSERT INTO daily_metrics (date, metric, value, raw_measurement_count, aggregation_method, aggregated_at) "
        "VALUES ('2026-01-01', 'steps', 1, 1, 'sum', CURRENT_TIMESTAMP)"
    )
    conn.commit()
    result = invariant.verify(conn)
    assert any(i["issue"] == "orphaned_daily_row" for i in result["issues"])


def test_bulk_import_then_repair_then_verify(conn):
    # simulate a bulk load bypassing per-row trigger overhead expectations
    # by inserting many rows in one transaction; trigger still fires per row
    # here (this test only checks correctness, not throughput).
    for i in range(50):
        insert(conn, "steps", 10, f"2026-01-{(i % 28) + 1:02d}T08:00:00")
    assert invariant.verify(conn)["status"] == "ok"
    assert invariant.repair(conn)["status"] == "ok"


def test_analytics_sees_insert_immediately(conn):
    insert(conn, "mood", 7, "2026-09-17T08:00:00")
    value = conn.execute("SELECT value FROM daily_metrics WHERE date = '2026-09-17' AND metric = 'mood'").fetchone()[0]
    assert value == 7


def test_verify_reports_unsupported_metric_status_distinct_from_mismatch(conn):
    # A rule deleted after measurements were written for it (see #8) is the
    # one way this state can be reached outside of a restored/foreign
    # database — the INSERT trigger itself blocks this at write time
    # (test_missing_rule_rolls_back), so verify() is the only backstop.
    insert(conn, "steps", 1000, "2026-09-17T08:00:00")
    conn.execute("DELETE FROM aggregation_rules WHERE metric = 'steps'")
    conn.commit()
    result = invariant.verify(conn)
    assert result["status"] == "unsupported_metric"
    assert any(i["issue"] == "unsupported_metric" and i["metric"] == "steps" for i in result["issues"])


def test_repair_leaves_unsupported_metric_rows_untouched(conn):
    insert(conn, "steps", 1000, "2026-09-17T08:00:00")
    conn.execute("DELETE FROM aggregation_rules WHERE metric = 'steps'")
    conn.commit()
    result = invariant.repair(conn)
    # repair() can't invent a canonical value for a metric with no rule —
    # verify() after repair should still flag it, not silently drop it.
    assert result["status"] == "unsupported_metric"


def test_bulk_insert_measurements_rebuilds_projection(conn):
    rows = [{"metric": "steps", "value": 10, "timestamp": f"2026-01-{i + 1:02d}T08:00:00"} for i in range(20)]
    result = invariant.bulk_insert_measurements(conn, rows)
    assert result["status"] == "ok"
    assert conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 20
    for i in range(20):
        assert daily(conn, f"2026-01-{i + 1:02d}", "steps") == (10, 1)


def test_bulk_insert_measurements_rejects_unsupported_metric_atomically(conn):
    rows = [
        {"metric": "steps", "value": 10, "timestamp": "2026-01-01T08:00:00"},
        {"metric": "not_a_real_metric", "value": 1, "timestamp": "2026-01-01T08:00:00"},
    ]
    with pytest.raises(ValueError, match="not_a_real_metric"):
        invariant.bulk_insert_measurements(conn, rows)
    # Nothing from the batch should have landed -- not even the supported row.
    assert conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 0


def test_bulk_insert_then_normal_write_still_maintains_invariant(conn):
    rows = [{"metric": "steps", "value": 10, "timestamp": "2026-01-01T08:00:00"}]
    invariant.bulk_insert_measurements(conn, rows)
    # Triggers must be back in place after the bulk path finishes.
    insert(conn, "steps", 5, "2026-01-01T09:00:00")
    assert daily(conn, "2026-01-01", "steps") == (15, 2)
