"""Sample-adequacy evaluator (Dimension.SAMPLE).

Only observed quantities may block: raw n / n_paired / baseline length, temporal span, and a
zero-variance baseline. Estimated quantities (n_eff, baseline stability, baseline contamination)
are diagnostic: they can produce WEAK with can_block=False, never BLOCKING.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from .models import Dimension, DimensionResult, Status

# Registry thresholds (registry.yaml) override these; keys not in the registry live only here.
DEFAULTS: dict[str, Any] = {
    "min_n_per_window": 7,
    "min_n": 14,
    "min_span_days": 21,
    "min_n_paired": 20,
    "min_baseline_days": 28,
    "min_baseline_obs": 14,
    "min_n_eff": 10.0,  # diagnostic only
    "baseline_drift_max": 1.0,  # diagnostic only: |mean_half1 - mean_half2| / pooled sd
    "min_baseline_for_stability": 8,
    "contamination_z": 3.5,  # robust (MAD) z; diagnostic only
    "max_contamination_frac": 0.10,
}


@dataclass(frozen=True)
class _Check:
    name: str
    code: str
    observed: Any
    threshold: Any
    passed: bool
    can_block: bool  # True only for observed quantities

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "observed": self.observed,
            "threshold": self.threshold,
            "passed": self.passed,
            "can_block": self.can_block,
        }


def _finite(v: float | None) -> bool:
    return v is not None and math.isfinite(v)


def _at_least(name: str, code: str, observed: float, minimum: float, can_block: bool) -> _Check:
    return _Check(name, code, observed, minimum, observed >= minimum, can_block)


def _result(checks: list[_Check], not_assessed: list[str] | None = None, **details: Any) -> DimensionResult:
    failed = [c for c in checks if not c.passed]
    blocking = [c for c in failed if c.can_block]
    diagnostic = [c for c in failed if not c.can_block]
    if blocking:
        status, can_block = Status.BLOCKING, True
    elif diagnostic:
        # Diagnostic-only failure: never BLOCKING, and flagged so the resolver cannot treat it as one.
        status, can_block = Status.WEAK, False
    else:
        status, can_block = Status.ADEQUATE, True
    return DimensionResult(
        dimension=Dimension.SAMPLE,
        status=status,
        reason_codes=list(dict.fromkeys(c.code for c in blocking + diagnostic)),
        can_block=can_block,
        details={"checks": [c.as_dict() for c in checks], "not_assessed": not_assessed or [], **details},
    )


def _merge(thresholds: dict[str, Any] | None) -> dict[str, Any]:
    return {**DEFAULTS, **(thresholds or {})}


def _baseline_drift(baseline: Sequence[float | None], t: dict[str, Any]) -> float | None:
    """Half-to-half mean drift in pooled sd units; None if the baseline is too short to assess.

    Assumes `baseline` is in chronological order.
    """
    vals = [float(v) for v in baseline if _finite(v)]
    if len(vals) < max(t["min_baseline_for_stability"], 4):
        return None
    h = len(vals) // 2
    a, b = vals[:h], vals[h:]
    pooled = math.sqrt((statistics.variance(a) + statistics.variance(b)) / 2)
    diff = abs(statistics.mean(a) - statistics.mean(b))
    if pooled == 0:
        return 0.0 if diff == 0 else math.inf
    return diff / pooled


def evaluate_window_comparison(
    window_a: Sequence[float | None],
    window_b: Sequence[float | None],
    baseline: Sequence[float | None] | None = None,
    thresholds: dict[str, Any] | None = None,
) -> DimensionResult:
    """n per window blocks. Baseline stability (only if `baseline` is given) is diagnostic."""
    t = _merge(thresholds)
    checks = [
        _at_least(f"n_{name}", "n_below_minimum", sum(_finite(v) for v in w), t["min_n_per_window"], True)
        for name, w in (("window_a", window_a), ("window_b", window_b))
    ]
    not_assessed: list[str] = []
    if baseline is None:
        not_assessed.append("baseline_stability")
    else:
        drift = _baseline_drift(baseline, t)
        if drift is None:
            not_assessed.append("baseline_stability")
        else:
            checks.append(
                _Check(
                    "baseline_stability",
                    "baseline_unstable",
                    round(drift, 3) if math.isfinite(drift) else None,
                    t["baseline_drift_max"],
                    drift <= t["baseline_drift_max"],
                    False,
                )
            )
    return _result(checks, not_assessed)


def evaluate_trend(
    dates: Sequence[date],
    values: Sequence[float | None],
    thresholds: dict[str, Any] | None = None,
) -> DimensionResult:
    """n and temporal span both block. A missing value drops its date with it, so span is over kept points."""
    if len(dates) != len(values):
        raise ValueError("dates and values must be the same length")
    t = _merge(thresholds)
    kept = [d for d, v in zip(dates, values, strict=True) if _finite(v)]
    span = (max(kept) - min(kept)).days if kept else 0
    checks = [
        _at_least("n", "n_below_minimum", len(kept), t["min_n"], True),
        _at_least("span_days", "span_below_minimum", span, t["min_span_days"], True),
    ]
    return _result(checks)


def evaluate_correlation(
    x: Sequence[float | None],
    y: Sequence[float | None],
    n_eff: float | None = None,
    thresholds: dict[str, Any] | None = None,
) -> DimensionResult:
    """n_paired blocks. n_eff is an approximate estimate: diagnostic only, can_block=False."""
    if len(x) != len(y):
        raise ValueError("x and y must be the same length")
    t = _merge(thresholds)
    n_paired = sum(_finite(a) and _finite(b) for a, b in zip(x, y, strict=True))
    checks = [_at_least("n_paired", "n_paired_below_minimum", n_paired, t["min_n_paired"], True)]
    if n_eff is not None:
        ok = _finite(n_eff) and n_eff >= t["min_n_eff"]
        checks.append(
            _Check("n_eff", "n_eff_low", round(n_eff, 2) if _finite(n_eff) else None, t["min_n_eff"], ok, False)
        )
    return _result(checks)


def _contamination_frac(vals: Sequence[float], z_max: float) -> float | None:
    med = statistics.median(vals)
    mad = statistics.median(abs(v - med) for v in vals)
    if mad == 0:
        return None
    return sum(1 for v in vals if 0.6745 * abs(v - med) / mad > z_max) / len(vals)


def evaluate_anomaly(
    baseline: Sequence[float | None],
    thresholds: dict[str, Any] | None = None,
) -> DimensionResult:
    """`baseline` is daily-aligned (None = missing).

    Length (first to last observation, so a long window with a short history does not pass), observation count
    and zero variance block; contamination is diagnostic.
    """
    t = _merge(thresholds)
    observed = [i for i, v in enumerate(baseline) if _finite(v)]
    vals = [float(baseline[i]) for i in observed]
    span = observed[-1] - observed[0] + 1 if observed else 0
    checks = [
        _at_least("baseline_days", "baseline_too_short", span, t["min_baseline_days"], True),
        _at_least("baseline_obs", "baseline_too_few_observations", len(vals), t["min_baseline_obs"], True),
    ]
    not_assessed: list[str] = []
    if len(vals) >= 2:
        sd = statistics.stdev(vals)
        checks.append(_Check("baseline_variance", "baseline_zero_variance", sd, 0.0, sd > 0, True))
    else:
        not_assessed.append("baseline_variance")
    frac = _contamination_frac(vals, t["contamination_z"]) if len(vals) >= 5 else None
    if frac is None:
        not_assessed.append("baseline_contamination")
    else:
        checks.append(
            _Check(
                "baseline_contamination",
                "baseline_contaminated",
                round(frac, 3),
                t["max_contamination_frac"],
                frac <= t["max_contamination_frac"],
                False,
            )
        )
    return _result(checks, not_assessed)


_EVALUATORS: dict[str, Callable[..., DimensionResult]] = {
    "window_comparison": evaluate_window_comparison,
    "trend": evaluate_trend,
    "correlation": evaluate_correlation,
    "anomaly": evaluate_anomaly,
}


def evaluate_sample(analysis: str, thresholds: dict[str, Any] | None = None, **inputs: Any) -> DimensionResult:
    """Dispatch on analysis name. Pass `AnalysisSpec.thresholds` from the registry as `thresholds`."""
    try:
        fn = _EVALUATORS[analysis]
    except KeyError:
        raise ValueError(f"no sample evaluator for analysis {analysis!r}") from None
    return fn(thresholds=thresholds, **inputs)
