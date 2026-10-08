"""
ingest.py
=========
The canonical ingestion contract: the one shape every source adapter's observations must take before they
reach the database, and the validator that enforces it. An adapter turns a provider's export into
CanonicalRecords; nothing downstream (the projection, the analytics tools, data health) ever sees a provider
name in a branch, only records that mean the same thing whichever provider they came from.

A CanonicalRecord carries:
  metric            a key of metric_registry.METRICS
  timestamp         naive local wall-clock time, YYYY-MM-DDTHH:MM:SS (optionally with .mmm or .ffffff). The
                    projection buckets days with SQLite date(timestamp), so an offset here would shift evening
                    readings onto the next UTC day; the original offset belongs in `timezone`, not here.
  value             a finite int or float inside the metric's registry bounds, already in the canonical unit
  unit              the metric's registry unit. Known spellings are normalised ("count/min" -> "bpm"); a missing
                    unit takes the registry's, since the metric name already encodes it. A unit that needs a
                    conversion (lb, L) is rejected: converting values is the adapter's job, not the validator's.
  source            the device or service the reading came from, if the provider says; the pipeline stamps the
                    importer's name when it is missing
  source_type       free-form provenance such as "wearable", optional
  source_record_id  the provider's own id for the record, or a deterministic one the adapter derives; lets the
                    pipeline recognise the same record twice
  timezone          the record's original UTC offset ("+01:00") or IANA name ("Europe/Berlin"), shape-checked
                    only so validation does not depend on the platform's tz database
  quality           provider-reported confidence normalised to 0..1, None when the provider gives none

The importer and import time are not the adapter's to set; the pipeline adds them. Unknown keys are an error,
so a drifting adapter fails loudly instead of silently dropping data.

Framework-free apart from metric_registry. Nothing here touches the database.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, NamedTuple

from metric_registry import METRICS, metric_bounds

_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3}|\.\d{6})?$")
_OFFSET = re.compile(r"^(UTC|[+-](0\d|1[0-4]):[0-5]\d)$")
_IANA = re.compile(r"^[A-Za-z][A-Za-z_+\-]*(/[A-Za-z0-9_+\-]+)+$")
MAX_TEXT_LENGTH = 256

# Spellings accepted for a metric's canonical unit, keyed by that canonical unit, compared case-insensitively
# apart from the canonical spelling itself. Anything not listed needs a value conversion and is rejected.
UNIT_ALIASES: dict[str, frozenset[str]] = {
    "bpm": frozenset({"count/min", "beats/min", "beat/min"}),
    "mL": frozenset({"ml", "milliliter", "milliliters", "millilitre", "millilitres"}),
    "h": frozenset({"hr", "hrs", "hour", "hours"}),
    "min": frozenset({"minute", "minutes"}),
    "count": frozenset({"counts"}),
    "kg": frozenset({"kilogram", "kilograms"}),
    "ms": frozenset({"millisecond", "milliseconds"}),
}

_FIELDS = (
    "metric",
    "timestamp",
    "value",
    "unit",
    "source",
    "source_type",
    "source_record_id",
    "timezone",
    "quality",
)
_REQUIRED = ("metric", "timestamp", "value")


class RecordError(ValueError):
    """One record breaks the ingestion contract. The import continues without it; adapters and the pipeline
    count it as skipped, the same way they treat a row they cannot parse."""


@dataclass(frozen=True)
class CanonicalRecord:
    metric: str
    timestamp: str
    value: float
    unit: str | None = None
    source: str | None = None
    source_type: str | None = None
    source_record_id: str | None = None
    timezone: str | None = None
    quality: float | None = None

    def to_row(self) -> dict[str, Any]:
        """The measurement-row dict the import path takes: every contract field, None where absent."""
        return asdict(self)


class NormalizedBatch(NamedTuple):
    records: list[CanonicalRecord]
    rejected: list[tuple[int, str]]  # (index in the input, reason), in input order


def _text(raw: Mapping[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RecordError(f"{key} must be text, got {type(value).__name__}")
    value = value.strip()
    if not value:
        return None
    if len(value) > MAX_TEXT_LENGTH:
        raise RecordError(f"{key} is longer than {MAX_TEXT_LENGTH} characters")
    return value


def _number(raw: Mapping[str, Any], key: str) -> float:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RecordError(f"{key} must be a number, got {value!r}")
    if not math.isfinite(value):
        raise RecordError(f"{key} must be finite, got {value!r}")
    return value


def _timestamp(raw: Mapping[str, Any]) -> str:
    value = raw.get("timestamp")
    if value is None or (isinstance(value, str) and not value.strip()):
        raise RecordError("missing timestamp")
    if not isinstance(value, str):
        raise RecordError(f"timestamp must be text, got {type(value).__name__}")
    value = value.strip()
    if not _TIMESTAMP.match(value):
        raise RecordError(
            f"timestamp {value!r} must be naive local time like 2026-01-31T08:30:00 (no UTC offset; "
            "put the offset in timezone)"
        )
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise RecordError(f"timestamp {value!r} is not a real date and time") from exc
    return value


def _unit(metric: str, raw: Mapping[str, Any]) -> str | None:
    canonical = METRICS[metric].unit
    given = _text(raw, "unit")
    if given is None:
        return canonical
    if canonical is None:
        raise RecordError(f"{metric} has no unit, got {given!r}")
    if given == canonical or given.lower() == canonical.lower() or given.lower() in UNIT_ALIASES.get(canonical, ()):
        return canonical
    raise RecordError(f"{metric} must be in {canonical}, got {given!r}; convert the value in the adapter")


def _timezone(raw: Mapping[str, Any]) -> str | None:
    value = _text(raw, "timezone")
    if value is None:
        return None
    if not (_OFFSET.match(value) or _IANA.match(value)):
        raise RecordError(f"timezone {value!r} must be an offset like +01:00 or a name like Europe/Berlin")
    return value


def _quality(raw: Mapping[str, Any]) -> float | None:
    if raw.get("quality") is None:
        return None
    value = _number(raw, "quality")
    if not 0 <= value <= 1:
        raise RecordError(f"quality must be between 0 and 1, got {value!r}")
    return float(value)


def normalize_record(raw: Mapping[str, Any]) -> CanonicalRecord:
    """Validate one adapter-produced dict and return it as a CanonicalRecord, normalising the unit.
    Raises RecordError, saying why, for anything that breaks the contract."""
    unknown = sorted(set(raw) - set(_FIELDS))
    if unknown:
        raise RecordError(f"unexpected field(s): {', '.join(unknown)}")
    for key in _REQUIRED:
        if raw.get(key) is None:
            raise RecordError(f"missing {key}")
    metric = raw["metric"]
    if not isinstance(metric, str) or metric not in METRICS:
        raise RecordError(f"unsupported metric {metric!r}")
    timestamp = _timestamp(raw)
    value = _number(raw, "value")
    low, high, label = metric_bounds()[metric]
    if not low <= value <= high:
        raise RecordError(f"{label} must be between {low} and {high}, got {value}")
    return CanonicalRecord(
        metric=metric,
        timestamp=timestamp,
        value=value,
        unit=_unit(metric, raw),
        source=_text(raw, "source"),
        source_type=_text(raw, "source_type"),
        source_record_id=_text(raw, "source_record_id"),
        timezone=_timezone(raw),
        quality=_quality(raw),
    )


def normalize_batch(raws: Iterable[Mapping[str, Any]]) -> NormalizedBatch:
    """normalize_record over a batch: valid records in input order, and the index and reason of every
    rejected one. One bad record never prevents the rest from loading."""
    records: list[CanonicalRecord] = []
    rejected: list[tuple[int, str]] = []
    for index, raw in enumerate(raws):
        try:
            records.append(normalize_record(raw))
        except RecordError as exc:
            rejected.append((index, str(exc)))
    return NormalizedBatch(records, rejected)


def drop_duplicate_ids(records: Iterable[CanonicalRecord]) -> tuple[list[CanonicalRecord], int]:
    """Keep the first record for each source_record_id and drop the repeats, returning (kept, dropped).
    Records without an id are never treated as duplicates of each other. Order is preserved."""
    seen: set[str] = set()
    kept: list[CanonicalRecord] = []
    dropped = 0
    for record in records:
        if record.source_record_id is not None:
            if record.source_record_id in seen:
                dropped += 1
                continue
            seen.add(record.source_record_id)
        kept.append(record)
    return kept, dropped
