"""
Canonical definition of the raw -> daily aggregation semantics.

For each (metric, day) exactly one source's observations are aggregated, never
a blend of devices. The winning source is the highest-ranked present source in
source_priority (a metric's own list, else the '*' list); with none ranked, the
source that observed the most distinct hours of the day, then the one with the
latest observation, then the source name (a missing source sorts last). The
order is written once, in _winner_order_by().

One semantic definition (`aggregation_case_sql`), three consumers:
  - generate_trigger_sql()  -> INSERT/UPDATE/DELETE triggers (write path)
  - _expected_projection_sql() -> shared by verify() and repair() (audit/rebuild)

Do not hand-write an alternate aggregation expression anywhere else.
Adding a new method requires updating AGGREGATION_METHODS here; nothing
else should need to change.
"""

AGGREGATION_METHODS = {"sum", "mean", "last"}

# source_priority.metric value for the list that applies to every metric
# without a list of its own.
GLOBAL_PRIORITY_SCOPE = "*"


def aggregation_case_sql(metric_sql: str, date_sql: str, source_sql: str, value_col: str = "value") -> str:
    """
    Build the canonical aggregation expression for a given (metric, date) key.

    source_sql is an SQL fragment for the resolved source of that key; only that
    source's measurements are aggregated (NULL-safe: a missing source matches a
    missing source). Every caller must pass one -- aggregating across sources is
    exactly the bug this parameter exists to prevent.

    metric_sql / date_sql are SQL fragments identifying the key to aggregate
    (e.g. "NEW.metric" / "date(NEW.timestamp)" in a trigger, or a column
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
                               WHERE metric = {metric_sql} AND date(timestamp) = {date_sql}
                                 AND source IS {source_sql})
            WHEN 'mean' THEN (SELECT AVG({value_col}) FROM measurements
                               WHERE metric = {metric_sql} AND date(timestamp) = {date_sql}
                                 AND source IS {source_sql})
            WHEN 'last' THEN (SELECT {value_col} FROM measurements
                               WHERE metric = {metric_sql} AND date(timestamp) = {date_sql}
                                 AND source IS {source_sql}
                               ORDER BY timestamp DESC, id DESC LIMIT 1)
        END
    )"""


def _priority_rank_sql(metric_sql: str, source_sql: str) -> str:
    """Rank of source_sql in the priority list that applies to metric_sql, or NULL
    if that source isn't listed. A metric with its own list in source_priority
    uses only that list; every other metric uses the '*' list. Both arguments
    must be qualified column references or NEW./OLD. references -- an unqualified
    name would bind to source_priority's own column inside the subquery."""
    return f"""(
        CASE WHEN EXISTS (SELECT 1 FROM source_priority WHERE metric = {metric_sql})
             THEN (SELECT rank FROM source_priority WHERE metric = {metric_sql} AND source = {source_sql})
             ELSE (SELECT rank FROM source_priority
                    WHERE metric = '{GLOBAL_PRIORITY_SCOPE}' AND source = {source_sql})
        END
    )"""


def _winner_order_by(rank_sql: str, coverage_sql: str, latest_sql: str, source_sql: str) -> str:
    """The one definition of which source wins a (metric, day): ranked sources
    first (lowest rank), then the most distinct hours observed, then the latest
    observation, then source name with a missing source last. Both the per-key
    trigger query and the set-based repair/verify query build their ORDER BY
    from this, each supplying its own expressions for the four inputs."""
    return (
        f"({rank_sql}) IS NULL, {rank_sql}, {coverage_sql} DESC, {latest_sql} DESC, "
        f"({source_sql}) IS NULL, {source_sql}"
    )


def _winner_sql(metric_sql: str, date_sql: str) -> str:
    """One-row query for a single (metric, day) key -- the trigger path. Yields
    (src, n_sources, rnk): the winning source, how many distinct sources have
    data for the key, and the winner's priority rank (NULL when unranked). Yields
    no row at all when the key has no measurements."""
    rank = _priority_rank_sql(metric_sql, "m.source")
    order_by = _winner_order_by(
        rank, "COUNT(DISTINCT strftime('%H', m.timestamp))", "MAX(m.timestamp)", "m.source"
    )
    return f"""SELECT m.source AS src, COUNT(*) OVER () AS n_sources, {rank} AS rnk
        FROM measurements m
        WHERE m.metric = {metric_sql} AND date(m.timestamp) = {date_sql}
        GROUP BY m.source
        ORDER BY {order_by}
        LIMIT 1"""


def _resolution_case_sql(n_sources_sql: str, rank_sql: str) -> str:
    """daily_metrics.resolution for a resolved key."""
    return (
        f"CASE WHEN {n_sources_sql} = 1 THEN 'single' "
        f"WHEN {rank_sql} IS NOT NULL THEN 'priority' ELSE 'fallback' END"
    )


def _upsert_key_sql(metric_sql: str, date_sql: str) -> str:
    """UPSERT of one (date, metric) key into daily_metrics. Used by all three
    triggers. The FROM clause is the key's winning-source query, which yields no
    row when no measurements remain for the key, so this is then a no-op (not a
    NULL-writing upsert) -- that case is handled by _cleanup_key_sql() instead,
    which deletes the row."""
    agg = aggregation_case_sql(metric_sql, date_sql, "w.src")
    resolution = _resolution_case_sql("w.n_sources", "w.rnk")
    return f"""
    INSERT INTO daily_metrics (date, metric, value, raw_measurement_count, aggregation_method, aggregated_at,
                               resolved_source, source_count, resolution)
    SELECT {date_sql}, {metric_sql}, {agg},
           (SELECT COUNT(*) FROM measurements
             WHERE metric = {metric_sql} AND date(timestamp) = {date_sql} AND source IS w.src),
           (SELECT method FROM aggregation_rules WHERE metric = {metric_sql}),
           CURRENT_TIMESTAMP,
           w.src, w.n_sources, {resolution}
    FROM ({_winner_sql(metric_sql, date_sql)}) AS w
    WHERE 1
    ON CONFLICT(date, metric) DO UPDATE SET
        value = excluded.value,
        raw_measurement_count = excluded.raw_measurement_count,
        aggregation_method = excluded.aggregation_method,
        aggregated_at = excluded.aggregated_at,
        resolved_source = excluded.resolved_source,
        source_count = excluded.source_count,
        resolution = excluded.resolution;
    """


def _cleanup_key_sql(metric_sql: str, date_sql: str) -> str:
    """Drop the daily_metrics row if no measurements remain for that key."""
    return f"""
    DELETE FROM daily_metrics
    WHERE date = {date_sql} AND metric = {metric_sql}
      AND NOT EXISTS (
          SELECT 1 FROM measurements
          WHERE metric = {metric_sql} AND date(timestamp) = {date_sql}
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
    new_metric, new_date = "NEW.metric", "date(NEW.timestamp)"
    old_metric, old_date = "OLD.metric", "date(OLD.timestamp)"

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


def _expected_projection_sql(metric_filter_sql: str = "") -> str:
    """
    WITH-clause + SELECT producing the canonical expected daily_metrics rows
    from raw measurements. Shared, verbatim, by verify() (diffed read-only
    against daily_metrics) and repair() (inserted to rebuild daily_metrics).
    Only covers metrics that have an aggregation_rules entry; see
    unsupported_metrics_sql() for the rest.

    Set-based (one grouped pass to pick each key's winning source) rather than
    the per-key query the triggers use, so a bulk rebuild doesn't rescan
    measurements once per key; the winner order itself comes from the same
    _winner_order_by() either way. metric_filter_sql, e.g. ' AND m.metric =
    :metric', restricts the rebuild to some metrics.
    """
    order_by = _winner_order_by("ps.rnk", "ps.coverage", "ps.latest", "ps.source")
    rank = _priority_rank_sql("m.metric", "m.source")
    agg = aggregation_case_sql("w.metric", "w.day", "w.src")
    resolution = _resolution_case_sql("w.n_sources", "w.rnk")
    return f"""
    WITH per_source AS (
        SELECT m.metric AS metric, date(m.timestamp) AS day, m.source AS source,
               COUNT(DISTINCT strftime('%H', m.timestamp)) AS coverage,
               MAX(m.timestamp) AS latest,
               {rank} AS rnk
        FROM measurements m
        WHERE EXISTS (SELECT 1 FROM aggregation_rules r WHERE r.metric = m.metric){metric_filter_sql}
        GROUP BY m.metric, date(m.timestamp), m.source
    ),
    ranked AS (
        SELECT ps.metric AS metric, ps.day AS day, ps.source AS source, ps.rnk AS rnk,
               COUNT(*) OVER (PARTITION BY ps.metric, ps.day) AS n_sources,
               ROW_NUMBER() OVER (PARTITION BY ps.metric, ps.day ORDER BY {order_by}) AS rn
        FROM per_source ps
    ),
    winners AS (
        SELECT metric, day, source AS src, rnk, n_sources FROM ranked WHERE rn = 1
    ),
    expected AS (
        SELECT
            w.day AS date,
            w.metric AS metric,
            {agg} AS value,
            (SELECT COUNT(*) FROM measurements mm
              WHERE mm.metric = w.metric AND date(mm.timestamp) = w.day
                AND mm.source IS w.src) AS raw_measurement_count,
            (SELECT method FROM aggregation_rules WHERE metric = w.metric) AS aggregation_method,
            w.src AS resolved_source,
            w.n_sources AS source_count,
            {resolution} AS resolution
        FROM winners w
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
       OR d.resolved_source IS NOT expected.resolved_source
       OR d.source_count != expected.source_count
       OR d.resolution != expected.resolution

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
    INSERT INTO daily_metrics (date, metric, value, raw_measurement_count, aggregation_method, aggregated_at,
                               resolved_source, source_count, resolution)
    SELECT date, metric, value, raw_measurement_count, aggregation_method, CURRENT_TIMESTAMP,
           resolved_source, source_count, resolution
    FROM expected;
    """


def generate_scoped_repair_statements() -> tuple[str, str]:
    """(delete_sql, insert_sql) that rebuild the projection for the one metric
    bound to :metric. Two separate statements, each meant for conn.execute(), so
    a caller can run them inside its own transaction (generate_repair_sql() is a
    script and commits)."""
    delete_sql = "DELETE FROM daily_metrics WHERE metric = :metric"
    insert_sql = f"""
    {_expected_projection_sql(" AND m.metric = :metric")}
    INSERT INTO daily_metrics (date, metric, value, raw_measurement_count, aggregation_method, aggregated_at,
                               resolved_source, source_count, resolution)
    SELECT date, metric, value, raw_measurement_count, aggregation_method, CURRENT_TIMESTAMP,
           resolved_source, source_count, resolution
    FROM expected;
    """
    return delete_sql, insert_sql
