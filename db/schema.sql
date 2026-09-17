-- Source of truth
CREATE TABLE IF NOT EXISTS measurements (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    metric      TEXT NOT NULL,
    value       REAL NOT NULL,
    recorded_at TEXT NOT NULL  -- ISO 8601 datetime
);
CREATE INDEX IF NOT EXISTS idx_measurements_metric_date
    ON measurements (metric, recorded_at);

-- Materialized projection of measurements, maintained transactionally by
-- db/triggers.sql. Never written to directly outside a trigger or repair().
CREATE TABLE IF NOT EXISTS daily_metrics (
    date                  TEXT NOT NULL,
    metric                TEXT NOT NULL,
    value                 REAL NOT NULL,
    raw_measurement_count INTEGER NOT NULL,
    aggregation_method    TEXT NOT NULL,
    aggregated_at         TEXT NOT NULL,
    PRIMARY KEY (date, metric)
);

-- Declarative metric -> aggregation method. Canonical source consumed by
-- db/aggregation.py to generate trigger DDL, verify SQL, and repair SQL.
CREATE TABLE IF NOT EXISTS aggregation_rules (
    metric TEXT PRIMARY KEY,
    method TEXT NOT NULL CHECK (method IN ('sum', 'mean', 'last'))
);

INSERT OR IGNORE INTO aggregation_rules (metric, method) VALUES
    ('steps',           'sum'),
    ('water_ml',        'sum'),
    ('workout_minutes', 'sum'),
    ('hrv_ms',          'mean'),
    ('heart_rate',      'mean'),
    ('mood',            'mean'),
    ('weight_kg',       'last');
