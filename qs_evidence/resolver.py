"""Weakest-link resolver. Sees only DimensionResult contracts + registry policy."""

from __future__ import annotations

from collections.abc import Sequence

from .models import TIER_RANK, ClaimDecision, ClaimTier, Dimension, DimensionResult, EvidenceProfile, Status
from .registry import AnalysisSpec


def _effective(r: DimensionResult) -> Status:
    # Diagnostics cannot block on their own; they cap.
    if r.status == Status.BLOCKING and not r.can_block:
        return Status.WEAK
    return r.status


def resolve(profile: EvidenceProfile, spec: AnalysisSpec) -> ClaimDecision:
    blocking = capped = negligible = False
    factors: list[str] = []

    for dim in Dimension:  # deterministic order
        policy = spec.dimensions.get(dim)
        if policy is None:
            continue
        r = profile.get(dim)
        if r is None or r.status == Status.NOT_ASSESSED:
            if policy.on_not_assessed == "cap":
                capped = True
                factors.append(f"{dim.value}_not_assessed")
            continue
        s = _effective(r)
        if s == Status.BLOCKING:
            blocking = True
        elif s in (Status.WEAK, Status.CONCERN):
            capped = True
        elif s == Status.NEGLIGIBLE:
            negligible = True
        if s != Status.ADEQUATE:
            factors += [c for c in r.reason_codes if c not in factors]

    if blocking:
        tier = ClaimTier.INSUFFICIENT
    elif capped:  # data problems outrank "small effect"
        tier = ClaimTier.SUGGESTIVE
    elif negligible:
        tier = ClaimTier.DETECTABLE_NOT_MEANINGFUL
    else:
        tier = ClaimTier.SUPPORTED

    return ClaimDecision(tier=tier, limiting_factors=factors, permitted_phrasing_class=tier.value)


def combine_decisions(decisions: Sequence[ClaimDecision]) -> ClaimDecision:
    """Weakest-of-N: the single composite decision primitive for a result built from N assessed
    sub-claims (e.g. explain_metric_change combining a headline anomaly claim, a trend claim, and
    however many correlation claims it surfaced).

    Takes only already-resolved ClaimDecision objects — never raw confidence, sample_confidence, or
    any other descriptive field. That keeps a legacy field structurally unable to reach a composite
    decision, the same way resolve() keeps it out of a single-analysis one: neither function has a
    parameter a legacy field could be passed through even by accident.

    N >= 1, and the result never depends on the order of `decisions` (see
    tests/test_resolver.py::test_combine_decisions_is_permutation_invariant) — callers can assemble
    the sequence in whatever order is convenient (e.g. headline claim first, correlations appended
    as found) without affecting the outcome.
    """
    if not decisions:
        raise ValueError("combine_decisions requires at least one ClaimDecision")
    weakest_tier = min((d.tier for d in decisions), key=lambda t: TIER_RANK[t])
    # Sorted rather than first-seen-order: first-seen order depends on the caller's sequence order,
    # which is exactly the ordering dependence this function must not have.
    factors = sorted({f for d in decisions for f in d.limiting_factors})
    return ClaimDecision(tier=weakest_tier, limiting_factors=factors, permitted_phrasing_class=weakest_tier.value)
