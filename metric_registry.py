"""
metric_registry.py
==================
The canonical metric vocabulary: one MetricDefinition per metric, declared
once, from which the rest of the project derives what it used to restate.

A metric's key is its name everywhere (measurements.metric, the
aggregation_rules table, daily_metrics_wide column names, MCP tool
arguments). Each definition carries what the rest of the code needs to know
about it: its logical type, how raw measurements roll up into a day, its
plausible range (for validation), its canonical unit, and the label used in
validation errors.

Currently derived from this module:
  - logic.METRIC_BOUNDS            (validation ranges + labels)
  - server.METRIC_COLUMNS          (the metric vocabulary, in order)
  - server.INT_METRIC_COLUMNS      (metrics reported as whole numbers)

Still declared by hand elsewhere -- tests/test_metric_registry.py fails if
any of them stops agreeing with this registry:
  - db/schema.sql's aggregation_rules seed
  - server.DailyMetricsRow / server.HealthDataSummary fields
  - init_db's CSV column, alias, parser and label tables
  - logic's schema-migration column tuples

Stdlib only, so logic.py, server.py and init_db.py can all import it
without creating a dependency cycle.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

AGGREGATIONS = ("sum", "mean", "last")
VALUE_TYPES = ("int", "float")


@dataclass(frozen=True)
class MetricDefinition:
    key: str
    value_type: Literal["int", "float"]
    aggregation: Literal["sum", "mean", "last"]
    min_value: int | float
    max_value: int | float
    unit: str | None = None
    # Name used in validation errors, e.g. "resting_heart_rate (bpm)".
    # Defaults to the key.
    validation_label: str = ""

    def __post_init__(self) -> None:
        # Keys end up in SQL identifiers (daily_metrics_wide column names,
        # ALTER TABLE ... ADD COLUMN), so they must be plain identifiers.
        if not self.key.isidentifier():
            raise ValueError(f"metric key must be a plain identifier, got {self.key!r}")
        if self.value_type not in VALUE_TYPES:
            raise ValueError(f"{self.key}: value_type must be one of {VALUE_TYPES}, got {self.value_type!r}")
        if self.aggregation not in AGGREGATIONS:
            raise ValueError(f"{self.key}: aggregation must be one of {AGGREGATIONS}, got {self.aggregation!r}")
        if self.min_value > self.max_value:
            raise ValueError(f"{self.key}: min_value {self.min_value} is greater than max_value {self.max_value}")
        if not self.validation_label:
            object.__setattr__(self, "validation_label", self.key)


def build_registry(definitions: Iterable[MetricDefinition]) -> Mapping[str, MetricDefinition]:
    """Read-only, insertion-ordered key -> definition mapping. Duplicate
    keys are an error rather than a silent overwrite."""
    registry: dict[str, MetricDefinition] = {}
    for definition in definitions:
        if definition.key in registry:
            raise ValueError(f"duplicate metric key: {definition.key!r}")
        registry[definition.key] = definition
    return MappingProxyType(registry)


# Order matters: it is the order server.METRIC_COLUMNS selects and reports
# metrics in (read_health_data columns, CSV export header, summaries).
#
# Ranges are deliberately generous -- meant to catch obvious mistakes (unit
# confusion, a slipped decimal point, a fat-fingered extra digit) rather than
# to police what's "normal". mood is fixed at 1-10 so the scale is consistent
# across every log_daily_metric call.
METRICS = build_registry(
    [
        MetricDefinition("steps", "int", "sum", 0, 200_000, unit="count"),
        MetricDefinition("sleep_hours", "float", "sum", 0, 24, unit="h"),
        MetricDefinition(
            "resting_heart_rate",
            "int",
            "mean",
            20,
            250,
            unit="bpm",
            validation_label="resting_heart_rate (bpm)",
        ),
        MetricDefinition("weight_kg", "float", "last", 1, 500, unit="kg"),
        MetricDefinition("workout_minutes", "int", "sum", 0, 1440, unit="min"),
        MetricDefinition(
            "mood",
            "int",
            "mean",
            1,
            10,
            validation_label="mood (expected on a 1-10 scale)",
        ),
        MetricDefinition("water_ml", "int", "sum", 0, 10_000, unit="mL"),
        MetricDefinition(
            "heart_rate",
            "int",
            "mean",
            20,
            250,
            unit="bpm",
            validation_label="heart_rate (bpm)",
        ),
        MetricDefinition(
            "hrv_ms",
            "float",
            "mean",
            0,
            300,
            unit="ms",
            validation_label="hrv_ms (ms)",
        ),
    ]
)

METRIC_KEYS: tuple[str, ...] = tuple(METRICS)

INT_METRIC_KEYS: frozenset[str] = frozenset(key for key, m in METRICS.items() if m.value_type == "int")


def metric_bounds() -> dict[str, tuple[int | float, int | float, str]]:
    """metric key -> (min_value, max_value, validation_label), in registry
    order. This is the shape logic.METRIC_BOUNDS has always had."""
    return {key: (m.min_value, m.max_value, m.validation_label) for key, m in METRICS.items()}
