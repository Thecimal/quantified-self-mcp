"""Temporal-distribution evaluator. Input: daily-aligned values (None = missing)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any

from .models import Dimension, DimensionResult, Status

DEFAULTS = {
    "max_gap_days": 4,
    "min_third_coverage": 0.5,
    "max_weekday_weekend_coverage_diff": 0.35,
    "min_observations": 3,
}


def _cov(flags: Sequence[bool]) -> float | None:
    return sum(flags) / len(flags) if flags else None


def evaluate_temporal(
    values: Sequence[float | None], start: date, thresholds: dict[str, Any] | None = None
) -> DimensionResult:
    t = {**DEFAULTS, **(thresholds or {})}
    n = len(values)
    obs = [v is not None for v in values]
    if sum(obs) < t["min_observations"]:
        return DimensionResult(
            dimension=Dimension.TEMPORAL,
            status=Status.NOT_ASSESSED,
            reason_codes=["too_few_observations_to_assess"],
            details={"n_obs": sum(obs)},
        )

    # longest gap and where it sits
    longest, cur, longest_end = 0, 0, -1
    for i, o in enumerate(obs):
        cur = 0 if o else cur + 1
        if cur > longest:
            longest, longest_end = cur, i
    thirds = [_cov([o for i, o in enumerate(obs) if i * 3 // n == k]) for k in range(3)]

    wd = [o for i, o in enumerate(obs) if (start + timedelta(days=i)).weekday() < 5]
    we = [o for i, o in enumerate(obs) if (start + timedelta(days=i)).weekday() >= 5]
    wd_cov, we_cov = _cov(wd), _cov(we)

    reasons: list[str] = []
    if longest > t["max_gap_days"]:
        reasons.append("recent_window_gap" if longest_end >= n * 2 // 3 else "long_gap")
    if min(c for c in thirds if c is not None) < t["min_third_coverage"]:
        reasons.append("uneven_coverage_across_window")
    if wd_cov is not None and we_cov is not None and abs(wd_cov - we_cov) > t["max_weekday_weekend_coverage_diff"]:
        reasons.append("weekday_weekend_imbalance")

    return DimensionResult(
        dimension=Dimension.TEMPORAL,
        status=Status.WEAK if reasons else Status.ADEQUATE,
        reason_codes=reasons,
        details={
            "longest_gap_days": longest,
            "third_coverage": [round(c, 2) if c is not None else None for c in thirds],
            "weekday_coverage": wd_cov,
            "weekend_coverage": we_cov,
        },
    )
