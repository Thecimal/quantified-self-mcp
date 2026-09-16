"""
evidence.py
===========
Computes a reusable Evidence/Coverage object for a single metric's time
series: how much of the requested range actually has data, where the gaps
are, how fresh the most recent observation is, and a conservative overall
confidence label. Every Layer-2/Layer-3 analytics tool in server.py attaches
one of these to its result, so a computed number (a baseline, a trend, a
correlation) is never reported without the coverage it's based on.

Framework-free (standard library only), mirroring analytics.py: takes an
already-fetched list[analytics.Point] plus the requested range, and does no
database access of its own.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
from typing import NamedTuple

from analytics import Point


class Gap(NamedTuple):
    start: date
    end: date
    days: int


def _find_gaps(observed: set[date], expected: Sequence[date]) -> list[Gap]:
    """Collapse missing dates into contiguous gap ranges."""
    missing = [d for d in expected if d not in observed]
    if not missing:
        return []
    gaps: list[Gap] = []
    gap_start = prev = missing[0]
    for d in missing[1:]:
        if (d - prev).days > 1:
            gaps.append(Gap(gap_start, prev, (prev - gap_start).days + 1))
            gap_start = d
        prev = d
    gaps.append(Gap(gap_start, prev, (prev - gap_start).days + 1))
    return gaps


def _confidence_label(coverage_ratio: float, recent_gap_days: int) -> str:
    """Conservative on purpose: a metric can have plenty of data points and
    still not deserve "high" confidence if there's a gap sitting right up
    against the end of the requested range (i.e. against "now").
    """
    if coverage_ratio >= 0.85 and recent_gap_days <= 2:
        return "high"
    if coverage_ratio >= 0.6 and recent_gap_days <= 7:
        return "moderate"
    return "low"


def _coverage_confidence(coverage_percent: float) -> str:
    """Simpler 3-bucket classifier for a multi-metric summary, where there's
    no single "recency" to anchor on the way there is for one metric's
    series (see _confidence_label). Same thresholds, coverage only.
    """
    if coverage_percent >= 85:
        return "high"
    if coverage_percent >= 60:
        return "moderate"
    return "low"


def build_coverage_summary(
    rows: Sequence[dict],
    start: date,
    end: date,
    metrics: Sequence[str],
) -> dict:
    """Build a multi-metric DataCoverage summary for a Layer-1 result (e.g.
    read_health_data) that reports several metrics side by side rather than
    one series — so it needs one coverage-percent per metric plus an
    overall figure, instead of build_evidence's single-metric gaps/
    freshness/recent_gap detail.

    ``rows`` is the already-fetched list of per-day dicts (one key per
    metric in ``metrics``, value ``None`` for days with nothing recorded
    for it, as returned by the daily_metrics query in server.py). ``metrics``
    should already have any private/redacted fields filtered out by the
    caller — this function has no notion of HEALTH_PRIVATE_FIELDS itself,
    and a metric with no data at all still gets an honest 0.0%, not None,
    since "never logged" is exactly the case this exists to surface.
    """
    expected_days = (end - start).days + 1
    days_with_data = len(rows)

    per_metric_pct: dict[str, float] = {}
    for metric in metrics:
        observed = sum(1 for r in rows if r.get(metric) is not None)
        per_metric_pct[metric] = round(100 * observed / expected_days, 1) if expected_days else 0.0

    overall_pct = round(100 * days_with_data / expected_days, 1) if expected_days else 0.0
    # Confidence reflects the metrics actually present, not "any metric
    # logged" (overall_pct) — a day with just one of nine metrics counts
    # toward overall_pct but shouldn't make the summary look complete.
    basis_pct = round(sum(per_metric_pct.values()) / len(per_metric_pct), 1) if per_metric_pct else overall_pct

    return {
        "period": f"{start.isoformat()}/{end.isoformat()}",
        "days_expected": expected_days,
        "days_with_data": days_with_data,
        "coverage_percent": overall_pct,
        "missing_days": expected_days - days_with_data,
        "metrics": per_metric_pct,
        "confidence": _coverage_confidence(basis_pct),
    }


def build_evidence(series: list[Point], start: date, end: date) -> dict:
    """Build an Evidence/Coverage dict for a metric series already fetched
    over [start, end] (inclusive).

    Returned as a plain dict — like analytics.py's own functions — so this
    module has zero opinion about Pydantic/FastMCP; server.py is
    responsible for wrapping it into its Evidence output model via
    ``Evidence(**build_evidence(series, start, end))``.
    """
    expected_days = (end - start).days + 1
    expected_dates = [start + timedelta(days=i) for i in range(expected_days)]

    observed_dates = sorted({p.day for p in series})
    observed_days = len(observed_dates)
    coverage_ratio = round(observed_days / expected_days, 4) if expected_days else 0.0

    gaps = _find_gaps(set(observed_dates), expected_dates)

    freshness_days = (end - observed_dates[-1]).days if observed_dates else None

    recent_gap_days = 0
    if gaps and gaps[-1].end == end:
        recent_gap_days = gaps[-1].days

    return {
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "observed_start": observed_dates[0].isoformat() if observed_dates else None,
        "observed_end": observed_dates[-1].isoformat() if observed_dates else None,
        "expected_days": expected_days,
        "observed_days": observed_days,
        "coverage_ratio": coverage_ratio,
        "missing_days": expected_days - observed_days,
        "measurement_count": len(series),
        "gaps": [{"start": g.start.isoformat(), "end": g.end.isoformat(), "days": g.days} for g in gaps],
        "freshness_days": freshness_days,
        "recent_gap_days": recent_gap_days,
        "confidence": _confidence_label(coverage_ratio, recent_gap_days),
    }
