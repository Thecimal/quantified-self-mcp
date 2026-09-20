"""Missingness evaluator: coverage, clustering (runs test), MNAR association (3-state)."""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Sequence
from typing import Any

from .models import Dimension, DimensionResult, Status

DEFAULTS = {
    "min_overall_coverage": 0.6,
    "runs_z_threshold": -1.96,
    "mnar_min_effect_d": 0.5,
    "mnar_alpha": 0.05,
    "mnar_min_group_n": 4,
    "mnar_permutations": 2000,
}


def _runs_z(missing: Sequence[bool]) -> float | None:
    """Wald-Wolfowitz z on the missing indicator. z << 0 = fewer runs than random = clustered."""
    n1, n = sum(missing), len(missing)
    n2 = n - n1
    if n1 < 3 or n2 < 3:
        return None
    runs = 1 + sum(1 for a, b in zip(missing, missing[1:], strict=False) if a != b)
    mu = 2 * n1 * n2 / n + 1
    var = (mu - 1) * (mu - 2) / (n - 1)
    return (runs - mu) / math.sqrt(var) if var > 0 else None


def _assess_mnar(missing: Sequence[bool], cov: Sequence[float | None], t) -> dict[str, Any]:
    a = [c for m, c in zip(missing, cov, strict=False) if m and c is not None]
    b = [c for m, c in zip(missing, cov, strict=False) if not m and c is not None]
    k = t["mnar_min_group_n"]
    if len(a) < k or len(b) < k:
        return {"state": "not_assessed", "reason": "too_few_covariate_values"}
    sd = math.sqrt(
        ((len(a) - 1) * statistics.variance(a) + (len(b) - 1) * statistics.variance(b)) / (len(a) + len(b) - 2)
    )
    obs = statistics.mean(a) - statistics.mean(b)
    d = obs / sd if sd > 0 else 0.0
    rng, pool, na, hits = random.Random(0), a + b, len(a), 0
    for _ in range(t["mnar_permutations"]):
        rng.shuffle(pool)
        if abs(statistics.mean(pool[:na]) - statistics.mean(pool[na:])) >= abs(obs):
            hits += 1
    p = (hits + 1) / (t["mnar_permutations"] + 1)
    found = abs(d) >= t["mnar_min_effect_d"] and p < t["mnar_alpha"]
    return {
        "state": "association_detected" if found else "no_association_found",
        "std_diff": round(d, 2),
        "p_perm": round(p, 4),
    }


def evaluate_missingness(
    values: Sequence[float | None],
    covariate: Sequence[float | None] | None = None,
    covariate_name: str = "covariate",
    thresholds: dict[str, Any] | None = None,
) -> DimensionResult:
    t = {**DEFAULTS, **(thresholds or {})}
    missing = [v is None for v in values]
    coverage = 1 - sum(missing) / len(missing)
    z = _runs_z(missing)
    mnar = (
        _assess_mnar(missing, covariate, t)
        if covariate is not None
        else {"state": "not_assessed", "reason": "no_covariate_provided"}
    )

    reasons: list[str] = []
    status = Status.ADEQUATE
    if coverage < t["min_overall_coverage"]:
        reasons.append("coverage_below_threshold")
        status = Status.WEAK
    if z is not None and z < t["runs_z_threshold"]:
        reasons.append("clustered_missing")
        status = Status.WEAK  # temporal issue, NOT mnar
    if mnar["state"] == "association_detected":
        reasons.append(f"missing_correlates_with_{covariate_name}")
        status = Status.CONCERN

    return DimensionResult(
        dimension=Dimension.MISSINGNESS,
        status=status,
        reason_codes=reasons,
        details={"coverage": round(coverage, 3), "runs_z": None if z is None else round(z, 2), "mnar": mnar},
    )
