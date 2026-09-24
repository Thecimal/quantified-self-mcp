-- Source of truth. Column set matches logic.py's MEASUREMENTS_SCHEMA (schema
-- v3) + MEASUREMENT_PROVENANCE_COLUMNS (v4) exactly, so whichever of the two
-- creates this table first, the other's "IF NOT EXISTS"/guarded-ALTER path is
-- a true no-op rather than a second, divergent definition. See db/invariant.py
-- for why both paths need to agree.
CREATE TABLE IF NOT EXISTS measurements (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,  -- ISO 8601 datetime of the observation
    metric      TEXT NOT NULL,
    value       REAL NOT NULL,
    unit        TEXT,
    source      TEXT,
    source_type TEXT,
    importer    TEXT,
    imported_at TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_measurements_metric_timestamp
    ON measurements (metric, timestamp);
-- The projection SQL (db/aggregation.py) always looks measurements up as
-- (metric, date(timestamp), source). Without an index on that exact
-- expression every per-key recompute scans all of a metric's rows, which
-- made a 120k-row repair take ~20s; with it, ~0.1s.
CREATE INDEX IF NOT EXISTS idx_measurements_metric_day_source
    ON measurements (metric, date(timestamp), source);

-- Materialized projection of measurements, maintained transactionally by
-- the triggers in db/aggregation.py's generate_trigger_sql(). Never written
-- to directly outside a trigger or db.invariant.repair().
CREATE TABLE IF NOT EXISTS daily_metrics (
    date                  TEXT NOT NULL,
    metric                TEXT NOT NULL,
    value                 REAL NOT NULL,
    raw_measurement_count INTEGER NOT NULL,
    aggregation_method    TEXT NOT NULL,
    aggregated_at         TEXT NOT NULL,
    -- Which source's observations the value was computed from, how many
    -- distinct sources had data for this (date, metric), and how that
    -- source was chosen: 'single' (only one source present), 'priority'
    -- (the highest-ranked present source in source_priority) or
    -- 'fallback' (several sources, none ranked: most distinct hours
    -- observed, then latest observation, then source name).
    -- resolved_source is NULL when the winning rows carry no source
    -- (e.g. a CSV day total).
    resolved_source       TEXT,
    source_count          INTEGER NOT NULL DEFAULT 1,
    resolution            TEXT NOT NULL DEFAULT 'single'
        CHECK (resolution IN ('single', 'priority', 'fallback')),
    PRIMARY KEY (date, metric)
);

-- Declarative metric -> aggregation method. Canonical source consumed by
-- db/aggregation.py to generate trigger DDL, verify SQL, and repair SQL.
-- Treated as effectively immutable once seeded (see db/invariant.py) rather
-- than editable at runtime, so a rule change can never silently leave a
-- stale daily_metrics projection behind (task P0 section 8).
CREATE TABLE IF NOT EXISTS aggregation_rules (
    metric TEXT PRIMARY KEY,
    method TEXT NOT NULL CHECK (method IN ('sum', 'mean', 'last'))
);

-- One entry per metric server.py's fixed daily_metrics columns have ever
-- covered (steps, sleep_hours, ... hrv_ms), using the same method each
-- already used via logic.MEASUREMENT_AGGREGATION ("avg" there == "mean"
-- here). log_measurement's free-form metrics are only accepted once a rule
-- for them exists here (see #7: an INSERT with no matching rule aborts).
INSERT OR IGNORE INTO aggregation_rules (metric, method) VALUES
    ('steps',              'sum'),
    ('sleep_hours',        'sum'),
    ('resting_heart_rate', 'mean'),
    ('water_ml',           'sum'),
    ('workout_minutes',    'sum'),
    ('hrv_ms',             'mean'),
    ('heart_rate',         'mean'),
    ('mood',               'mean'),
    ('weight_kg',          'last');

-- Which source wins when a (metric, day) has measurements from more than
-- one source (see db/aggregation.py). One ordered list per metric, or the
-- '*' list for every metric without its own; a metric with its own list
-- uses only that list. Not seeded here on purpose: the default
-- ('*', 1, 'daily-log') is inserted once by schema migration 8, so
-- removing it is not undone on the next start. Change it through
-- db.invariant.set_source_priority(), which re-projects daily_metrics in
-- the same transaction; verify() flags a projection left stale by a
-- direct edit.
CREATE TABLE IF NOT EXISTS source_priority (
    metric TEXT NOT NULL,
    rank   INTEGER NOT NULL CHECK (rank >= 1),
    source TEXT NOT NULL,
    PRIMARY KEY (metric, rank),
    UNIQUE (metric, source)
);
