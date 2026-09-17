"""
Canonical definition of the raw -> daily aggregation semantics.

One semantic definition (`aggregation_case_sql`), three consumers:
  - generate_trigger_sql()  -> INSERT/UPDATE/DELETE triggers (write path)
  - _expected_projection_sql() -> shared by verify() and repair() (audit/rebuild)

Do not hand-write an alternate aggregation expression anywhere else.
Adding a new method requires updating AGGREGATION_METHODS here; nothing
else should need to change.
"""

AGGREGATION_METHODS = {"sum", "mean", "last"}


def aggregation_case_sql(metric_sql: str, date_sql: str, value_col: str = "value") -> str:
    """
    Build the canonical aggregation expression for a given (metric, date) key.

    metric_sql / date_sql are SQL fragments identifying the key to aggregate
    (e.g. "NEW.metric" / "date(NEW.recorded_at)" in a trigger, or a column
    reference in a CTE). No ELSE branch: an unrecognized method evaluates to
    NULL here on purpose. Trigger callers must guard with a RAISE(ABORT, ...)
    check before using this (RAISE is trigger-only); verify() must flag it
    explicitly via a LEFT JOIN against aggregation_rules, since it cannot use
    RAISE() outside a trigger.
    """
    assert AGGREGATION_METHODS == {"sum", "mean", "last"}, (
        "AGGREGATION_METHODS changed without updating aggregation_case_sql()"
    )
    return f"""(
        CASE (SELECT method FROM aggregation_rules WHERE metric = {metric_sql})
            WHEN 'sum'  THEN (SELECT SUM({value_col}) FROM measurements
                               WHERE metric = {metric_sql} AND date(recorded_at) = {date_sql})
            WHEN 'mean' THEN (SELECT AVG({value_col}) FROM measurements
                               WHERE metric = {metric_sql} AND date(recorded_at) = {date_sql})
            WHEN 'last' THEN (SELECT {value_col} FROM measurements
                               WHERE metric = {metric_sql} AND date(recorded_at) = {date_sql}
                               ORDER BY recorded_at DESC LIMIT 1)
        END
    )"""


def _upsert_key_sql(metric_sql: str, date_sql: str) -> str:
    """UPSERT of one (date, metric) key into daily_metrics. Used by all three
    triggers. Guarded by WHERE EXISTS so it's a no-op (not a NULL-writing
    upsert) when no measurements remain for the key -- that case is handled
    by _cleanup_key_sql() instead, which deletes the row."""
    agg = aggregation_case_sql(metric_sql, date_sql)
    exists_guard = (
        f"EXISTS (SELECT 1 FROM measurements WHERE metric = {metric_sql} "
        f"AND date(recorded_at) = {date_sql})"
    )
    return f"""
    INSERT INTO daily_metrics (date, metric, value, raw_measurement_count, aggregation_method, aggregated_at)
    SELECT {date_sql}, {metric_sql}, {agg},
           (SELECT COUNT(*) FROM measurements WHERE metric = {metric_sql} AND date(recorded_at) = {date_sql}),
           (SELECT method FROM aggregation_rules WHERE metric = {metric_sql}),
           CURRENT_TIMESTAMP
    WHERE {exists_guard}
    ON CONFLICT(date, metric) DO UPDATE SET
        value = excluded.value,
        raw_measurement_count = excluded.raw_measurement_count,
        aggregation_method = excluded.aggregation_method,
        aggregated_at = excluded.aggregated_at;
    """


def _cleanup_key_sql(metric_sql: str, date_sql: str) -> str:
    """Drop the daily_metrics row if no measurements remain for that key."""
    return f"""
    DELETE FROM daily_metrics
    WHERE date = {date_sql} AND metric = {metric_sql}
      AND NOT EXISTS (
          SELECT 1 FROM measurements
          WHERE metric = {metric_sql} AND date(recorded_at) = {date_sql}
      );
    """


def _missing_rule_guard_sql(metric_sql: str) -> str:
    # RAISE(ABORT, message) requires message to be a string literal in
    # SQLite's grammar -- it cannot be a concatenated expression -- so the
    # metric name isn't embedded here. Callers needing the metric name
    # (e.g. a caught IntegrityError) should inspect the failed INSERT/UPDATE
    # itself; verify()'s unsupported_metrics_sql() also reports it by name.
    return f"""
    SELECT RAISE(ABORT, 'no aggregation_rules entry for metric')
    WHERE NOT EXISTS (SELECT 1 FROM aggregation_rules WHERE metric = {metric_sql});
    """


def generate_trigger_sql() -> str:
    """AFTER INSERT/UPDATE/DELETE triggers. Fire inside the caller's transaction,
    so measurements and daily_metrics always commit together."""
    new_metric, new_date = "NEW.metric", "date(NEW.recorded_at)"
    old_metric, old_date = "OLD.metric", "date(OLD.recorded_at)"

    insert_trigger = f"""
    DROP TRIGGER IF EXISTS trg_measurements_ai;
    CREATE TRIGGER trg_measurements_ai
    AFTER INSERT ON measurements
    BEGIN
        {_missing_rule_guard_sql(new_metric)}
        {_upsert_key_sql(new_metric, new_date)}
    END;
    """

    update_trigger = f"""
    DROP TRIGGER IF EXISTS trg_measurements_au;
    CREATE TRIGGER trg_measurements_au
    AFTER UPDATE ON measurements
    BEGIN
        {_missing_rule_guard_sql(new_metric)}
        {_upsert_key_sql(old_metric, old_date)}
        {_cleanup_key_sql(old_metric, old_date)}
        {_upsert_key_sql(new_metric, new_date)}
    END;
    """

    delete_trigger = f"""
    DROP TRIGGER IF EXISTS trg_measurements_ad;
    CREATE TRIGGER trg_measurements_ad
    AFTER DELETE ON measurements
    BEGIN
        {_upsert_key_sql(old_metric, old_date)}
        {_cleanup_key_sql(old_metric, old_date)}
    END;
    """
    return insert_trigger + update_trigger + delete_trigger


def _expected_projection_sql() -> str:
    """
    WITH-clause + SELECT producing the canonical expected daily_metrics rows
    from raw measurements. Shared, verbatim, by verify() (diffed read-only
    against daily_metrics) and repair() (inserted to rebuild daily_metrics).
    Only covers metrics that have an aggregation_rules entry; see
    unsupported_metrics_sql() for the rest.
    """
    agg = aggregation_case_sql("days.metric", "days.day")
    return f"""
    WITH days AS (
        SELECT DISTINCT m.metric, date(m.recorded_at) AS day
        FROM measurements m
        WHERE EXISTS (SELECT 1 FROM aggregation_rules r WHERE r.metric = m.metric)
    ),
    expected AS (
        SELECT
            day AS date,
            metric,
            {agg} AS value,
            (SELECT COUNT(*) FROM measurements mm
              WHERE mm.metric = days.metric AND date(mm.recorded_at) = days.day) AS raw_measurement_count,
            (SELECT method FROM aggregation_rules WHERE metric = days.metric) AS aggregation_method
        FROM days
    )
    """


def unsupported_metrics_sql() -> str:
    """Metrics present in measurements with no aggregation_rules entry.
    verify() reports these explicitly since triggers block them at write
    time via RAISE(ABORT, ...), and RAISE() is unavailable in a plain SELECT."""
    return """
    SELECT DISTINCT m.metric AS metric
    FROM measurements m
    LEFT JOIN aggregation_rules r ON r.metric = m.metric
    WHERE r.metric IS NULL
    """


def generate_verify_sql() -> str:
    """Pure read-only audit. Diffs the canonical expected projection against
    daily_metrics in both directions, plus the unsupported-metric check.
    Never mutates."""
    return f"""
    {_expected_projection_sql()}
    SELECT expected.date AS date, expected.metric AS metric, 'missing_or_stale' AS issue,
           expected.value AS expected_value, d.value AS stored_value
    FROM expected
    LEFT JOIN daily_metrics d ON d.date = expected.date AND d.metric = expected.metric
    WHERE d.value IS NULL
       OR d.value != expected.value
       OR d.raw_measurement_count != expected.raw_measurement_count
       OR d.aggregation_method != expected.aggregation_method

    UNION ALL

    SELECT d.date, d.metric, 'orphaned_daily_row' AS issue,
           NULL AS expected_value, d.value AS stored_value
    FROM daily_metrics d
    LEFT JOIN expected e ON e.date = d.date AND e.metric = d.metric
    WHERE e.date IS NULL

    UNION ALL

    SELECT NULL AS date, metric, 'unsupported_metric' AS issue,
           NULL AS expected_value, NULL AS stored_value
    FROM ({unsupported_metrics_sql()});
    """


def generate_repair_sql() -> str:
    """Rebuild daily_metrics from scratch for every metric that has a rule.
    Leaves rows for unsupported metrics untouched (there is nothing correct
    to compute for them); run verify() after repair() to confirm those are
    the only remaining issues, if any."""
    return f"""
    DELETE FROM daily_metrics
    WHERE metric IN (SELECT metric FROM aggregation_rules);

    {_expected_projection_sql()}
    INSERT INTO daily_metrics (date, metric, value, raw_measurement_count, aggregation_method, aggregated_at)
    SELECT date, metric, value, raw_measurement_count, aggregation_method, CURRENT_TIMESTAMP
    FROM expected;
    """
