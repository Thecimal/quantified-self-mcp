"""Assessment orchestration: one call per analysis.

registry policy -> every applicable evaluator -> EvidenceProfile -> weakest-link resolver -> ClaimDecision.

Framework-free like the evaluators: inputs are (day, value) pairs (e.g. analytics.Point tuples) plus the date
range the analysis covered. Dimensions the registry lists for an analysis but that have no evaluator yet are
emitted explicitly as NOT_ASSESSED (never silently adequate), so the resolver caps the claim and the profile
says why.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from typing import Any

from .missingness import evaluate_missingness
from .models import ClaimDecision, Dimension, DimensionResult, EvidenceProfile, Status
from .registry import AnalysisSpec, load_registry
from .resolver import resolve
from .sample import (
    evaluate_anomaly,
    evaluate_baseline,
    evaluate_correlation,
    evaluate_trend,
    evaluate_window_comparison,
)
from .temporal import evaluate_temporal

NOT_IMPLEMENTED = "evaluator_not_implemented"

# Severity used only to pick which per-window result represents a dimension. NOT_ASSESSED outranks ADEQUATE:
# unknown must never look better than known-good.
_SEVERITY = {
    Status.ADEQUATE: 0,
    Status.NEGLIGIBLE: 0,
    Status.NOT_ASSESSED: 1,
    Status.WEAK: 2,
    Status.CONCERN: 3,
    Status.BLOCKING: 4,
}


@dataclass(frozen=True)
class Assessment:
    profile: EvidenceProfile
    decision: ClaimDecision


@lru_cache(maxsize=1)
def _registry() -> dict[str, AnalysisSpec]:
    return load_registry()


def _thresholds(spec: AnalysisSpec) -> dict[str, Any]:
    t = dict(spec.thresholds)
    # The registry names this per-window; the missingness evaluator names it overall.
    if "min_coverage_per_window" in t:
        t.setdefault("min_overall_coverage", t["min_coverage_per_window"])
    return t


def align(points: Iterable[tuple[date, float]], start: date, end: date) -> list[float | None]:
    """Daily-aligned values over [start, end] inclusive; None = no (finite) observation that day."""
    n = max((end - start).days + 1, 0)
    out: list[float | None] = [None] * n
    for day, value in points:
        i = (day - start).days
        if 0 <= i < n and value is not None and math.isfinite(value):
            out[i] = float(value)
    return out


def _days(start: date, n: int) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


def _not_assessed(dim: Dimension) -> DimensionResult:
    return DimensionResult(dimension=dim, status=Status.NOT_ASSESSED, reason_codes=[NOT_IMPLEMENTED])


def _worst(dim: Dimension, named: dict[str, DimensionResult]) -> DimensionResult:
    """Combine per-window results for one dimension: the worst window decides; every window is kept in details."""
    worst = max(named.values(), key=lambda r: _SEVERITY[r.status])
    codes: list[str] = []
    for r in named.values():
        if r.status != Status.ADEQUATE:
            codes += [c for c in r.reason_codes if c not in codes]
    return DimensionResult(
        dimension=dim,
        status=worst.status,
        reason_codes=codes,
        can_block=worst.can_block,
        details={"windows": {k: {"status": r.status.value, **r.details} for k, r in named.items()}},
    )


def _finish(
    analysis: str, metric: str, effect: dict[str, Any] | None, evaluated: dict[Dimension, DimensionResult]
) -> Assessment:
    spec = _registry()[analysis]
    dims = [evaluated.get(d) or _not_assessed(d) for d in Dimension if d in spec.dimensions]
    profile = EvidenceProfile(metric=metric, analysis=analysis, effect=effect or {}, dimensions=dims)
    return Assessment(profile=profile, decision=resolve(profile, spec))


def assess_trend(
    metric: str,
    points: Iterable[tuple[date, float]],
    start: date,
    end: date,
    effect: dict[str, Any] | None = None,
) -> Assessment:
    t = _thresholds(_registry()["trend"])
    values = align(points, start, end)
    return _finish(
        "trend",
        metric,
        effect,
        {
            Dimension.SAMPLE: evaluate_trend(_days(start, len(values)), values, t),
            Dimension.TEMPORAL: evaluate_temporal(values, start, t),
            Dimension.MISSINGNESS: evaluate_missingness(values, thresholds=t),
        },
    )


def assess_window_comparison(
    metric: str,
    points_a: Iterable[tuple[date, float]],
    range_a: tuple[date, date],
    points_b: Iterable[tuple[date, float]],
    range_b: tuple[date, date],
    effect: dict[str, Any] | None = None,
) -> Assessment:
    """Period A is the later/"current" window, period B the baseline it is measured against."""
    t = _thresholds(_registry()["window_comparison"])
    a, b = align(points_a, *range_a), align(points_b, *range_b)
    return _finish(
        "window_comparison",
        metric,
        effect,
        {
            Dimension.SAMPLE: evaluate_window_comparison(a, b, baseline=b, thresholds=t),
            Dimension.TEMPORAL: _worst(
                Dimension.TEMPORAL,
                {"period_a": evaluate_temporal(a, range_a[0], t), "period_b": evaluate_temporal(b, range_b[0], t)},
            ),
            Dimension.MISSINGNESS: _worst(
                Dimension.MISSINGNESS,
                {"period_a": evaluate_missingness(a, thresholds=t), "period_b": evaluate_missingness(b, thresholds=t)},
            ),
        },
    )


def _lag1(xs: Sequence[float | None]) -> float | None:
    pairs = [(p, q) for p, q in zip(xs, xs[1:], strict=False) if p is not None and q is not None]
    if len(pairs) < 5:
        return None
    try:
        return statistics.correlation(*zip(*pairs, strict=True))
    except statistics.StatisticsError:  # constant series
        return None


def lag1_n_eff(x: Sequence[float | None], y: Sequence[float | None], n_paired: int) -> float | None:
    """Approximate effective sample size, n * (1 - rx*ry) / (1 + rx*ry) with lag-1 autocorrelations.

    An estimate, so it is only ever handed to the sample evaluator as a diagnostic (can_block=False).
    Returns None when it cannot be estimated.
    """
    rx, ry = _lag1(x), _lag1(y)
    if rx is None or ry is None:
        return None
    prod = rx * ry
    if 1 + prod <= 1e-9:
        return float(n_paired)
    return min(float(n_paired), n_paired * (1 - prod) / (1 + prod))


def assess_correlation(
    metric_a: str,
    points_a: Iterable[tuple[date, float]],
    metric_b: str,
    points_b: Iterable[tuple[date, float]],
    start: date,
    end: date,
    lag_days: int = 0,
    effect: dict[str, Any] | None = None,
) -> Assessment:
    """Judged on complete pairs, not per series. b is shifted so a[day] pairs with b[day + lag_days]."""
    t = _thresholds(_registry()["correlation"])
    x = align(points_a, start, end)
    y = align(points_b, start + timedelta(days=lag_days), end + timedelta(days=lag_days))
    paired = [p if p is not None and q is not None else None for p, q in zip(x, y, strict=True)]
    n_paired = sum(v is not None for v in paired)
    n_eff = lag1_n_eff(x, y, n_paired) if n_paired else None
    return _finish(
        "correlation",
        f"{metric_a}~{metric_b}",
        effect,
        {
            Dimension.SAMPLE: evaluate_correlation(x, y, n_eff=n_eff, thresholds=t),
            Dimension.TEMPORAL: evaluate_temporal(paired, start, t),
            Dimension.MISSINGNESS: evaluate_missingness(paired, thresholds=t),
        },
    )


def assess_anomaly(
    metric: str,
    points: Iterable[tuple[date, float]],
    start: date,
    end: date,
    effect: dict[str, Any] | None = None,
) -> Assessment:
    """The window is its own baseline, which is how analytics.detect_anomalies scores it."""
    t = _thresholds(_registry()["anomaly"])
    values = align(points, start, end)
    return _finish(
        "anomaly",
        metric,
        effect,
        {
            Dimension.SAMPLE: evaluate_anomaly(values, t),
            Dimension.TEMPORAL: evaluate_temporal(values, start, t),
        },
    )


def assess_baseline(
    metric: str,
    points: Iterable[tuple[date, float]],
    start: date,
    end: date,
    effect: dict[str, Any] | None = None,
) -> Assessment:
    """A "what's normal" claim over the window: sample adequacy, temporal spread and missingness."""
    t = _thresholds(_registry()["baseline"])
    values = align(points, start, end)
    return _finish(
        "baseline",
        metric,
        effect,
        {
            Dimension.SAMPLE: evaluate_baseline(values, t),
            Dimension.TEMPORAL: evaluate_temporal(values, start, t),
            Dimension.MISSINGNESS: evaluate_missingness(values, thresholds=t),
        },
    )
