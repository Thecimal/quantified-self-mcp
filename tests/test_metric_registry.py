"""
Tests for metric_registry.py.

The registry is the single declaration of the metric vocabulary. Some
consumers still restate it by hand (the pydantic models in server.py,
db/schema.sql's aggregation_rules seed, init_db's CSV tables, logic's
migration column tuples); the drift tests below fail when any of them stops
agreeing with it, so adding a metric to the registry without updating those
places is caught here rather than in production.
"""

import sqlite3
import sys
import typing
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import init_db
import logic
from metric_registry import (
    INT_METRIC_KEYS,
    METRIC_KEYS,
    METRICS,
    MetricDefinition,
    build_registry,
    metric_bounds,
)

# The nine metrics the project shipped with, exactly as they were declared
# before the registry existed: (value_type, aggregation, min, max, label).
# Pins that moving the declarations into the registry changed nothing.
LEGACY_METRICS = {
    "steps": ("int", "sum", 0, 200_000, "steps"),
    "sleep_hours": ("float", "sum", 0, 24, "sleep_hours"),
    "resting_heart_rate": ("int", "mean", 20, 250, "resting_heart_rate (bpm)"),
    "weight_kg": ("float", "last", 1, 500, "weight_kg"),
    "workout_minutes": ("int", "sum", 0, 1440, "workout_minutes"),
    "mood": ("int", "mean", 1, 10, "mood (expected on a 1-10 scale)"),
    "water_ml": ("int", "sum", 0, 10_000, "water_ml"),
    "heart_rate": ("int", "mean", 20, 250, "heart_rate (bpm)"),
    "hrv_ms": ("float", "mean", 0, 300, "hrv_ms (ms)"),
}


@pytest.fixture
def server_module(tmp_path, monkeypatch):
    # server.py reads HEALTH_DB_PATH / HEALTH_PRIVATE_FIELDS at import time;
    # same pattern as test_server.py's health_db fixture.
    monkeypatch.setenv("HEALTH_DB_PATH", str(tmp_path / "health.db"))
    monkeypatch.delenv("HEALTH_PRIVATE_FIELDS", raising=False)
    sys.modules.pop("server", None)
    import server

    return server


def _base_type(annotation):
    args = [a for a in typing.get_args(annotation) if a is not type(None)]
    assert len(args) == 1, annotation
    return args[0]


# --- the registry itself ----------------------------------------------------


def test_registry_starts_with_the_legacy_metrics_in_their_original_order():
    assert list(METRIC_KEYS[: len(LEGACY_METRICS)]) == list(LEGACY_METRICS)


def test_legacy_metric_definitions_are_unchanged():
    for key, (value_type, aggregation, low, high, label) in LEGACY_METRICS.items():
        m = METRICS[key]
        assert (m.value_type, m.aggregation, m.min_value, m.max_value, m.validation_label) == (
            value_type,
            aggregation,
            low,
            high,
            label,
        ), key


def test_registry_is_read_only():
    with pytest.raises(TypeError):
        METRICS["extra"] = MetricDefinition(key="extra", value_type="int", aggregation="sum", min_value=0, max_value=1)


def test_definition_rejects_inverted_range():
    with pytest.raises(ValueError, match="min_value"):
        MetricDefinition(key="x", value_type="int", aggregation="sum", min_value=5, max_value=1)


def test_definition_rejects_unknown_aggregation():
    with pytest.raises(ValueError, match="aggregation"):
        MetricDefinition(key="x", value_type="int", aggregation="median", min_value=0, max_value=1)


def test_definition_rejects_unknown_value_type():
    with pytest.raises(ValueError, match="value_type"):
        MetricDefinition(key="x", value_type="str", aggregation="sum", min_value=0, max_value=1)


def test_definition_rejects_key_that_is_not_a_plain_identifier():
    with pytest.raises(ValueError, match="identifier"):
        MetricDefinition(key="x; DROP TABLE t", value_type="int", aggregation="sum", min_value=0, max_value=1)


def test_definition_label_defaults_to_key():
    m = MetricDefinition(key="vo2_max", value_type="float", aggregation="last", min_value=5, max_value=100)
    assert m.validation_label == "vo2_max"


def test_build_registry_rejects_duplicate_keys():
    m = MetricDefinition(key="x", value_type="int", aggregation="sum", min_value=0, max_value=1)
    with pytest.raises(ValueError, match="duplicate"):
        build_registry([m, m])


# --- derived constants ------------------------------------------------------


def test_logic_metric_bounds_is_derived_from_the_registry():
    assert logic.METRIC_BOUNDS == metric_bounds()
    assert list(logic.METRIC_BOUNDS) == list(METRIC_KEYS)
    for key in LEGACY_METRICS:
        _, _, low, high, label = LEGACY_METRICS[key]
        assert logic.METRIC_BOUNDS[key] == (low, high, label)


def test_server_constants_are_derived_from_the_registry(server_module):
    assert server_module.METRIC_COLUMNS == list(METRIC_KEYS)
    assert server_module.INT_METRIC_COLUMNS == INT_METRIC_KEYS
    assert INT_METRIC_KEYS == {k for k, v in LEGACY_METRICS.items() if v[0] == "int"}


# --- hand-maintained consumers must agree with the registry -----------------


def test_aggregation_rules_seed_matches_registry():
    conn = sqlite3.connect(":memory:")
    logic.ensure_schema(conn)
    rules = dict(conn.execute("SELECT metric, method FROM aggregation_rules"))
    assert rules == {key: m.aggregation for key, m in METRICS.items()}


def test_v6_migration_column_tuple_matches_registry():
    assert set(logic._V6_DAILY_METRICS_COLUMNS) == set(METRICS)


def test_init_db_tables_cover_exactly_the_registry_metrics():
    keys = set(METRICS)
    assert set(init_db.ALL_METRIC_COLUMNS) == keys
    assert len(init_db.ALL_METRIC_COLUMNS) == len(keys)
    assert set(init_db._METRIC_PARSERS) == keys
    assert set(init_db._METRIC_LABELS) == keys
    assert set(init_db.COLUMN_ALIASES) == keys


def test_init_db_parsers_agree_with_registry_value_types():
    expected = {"int": init_db._to_int, "float": init_db._to_float}
    for key, m in METRICS.items():
        assert init_db._METRIC_PARSERS[key] is expected[m.value_type], key


def test_pydantic_models_match_registry(server_module):
    row_fields = server_module.DailyMetricsRow.model_fields
    assert set(row_fields) == {"date", *METRICS}
    py_types = {"int": int, "float": float}
    for key, m in METRICS.items():
        assert _base_type(row_fields[key].annotation) is py_types[m.value_type], key

    summary_fields = server_module.HealthDataSummary.model_fields
    assert set(summary_fields) == {"days_with_data", *METRICS}
