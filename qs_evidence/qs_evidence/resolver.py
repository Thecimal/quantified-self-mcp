"""Weakest-link resolver. Sees only DimensionResult contracts + registry policy."""
from __future__ import annotations
from .models import (ClaimDecision, ClaimTier, Dimension, DimensionResult,
                     EvidenceProfile, Status)
from .registry import AnalysisSpec


def _effective(r: DimensionResult) -> Status:
    # Diagnostics cannot block on their own; they cap.
    if r.status == Status.BLOCKING and not r.can_block:
        return Status.WEAK
    return r.status


def resolve(profile: EvidenceProfile, spec: AnalysisSpec) -> ClaimDecision:
    blocking = capped = negligible = False
    factors: list[str] = []

    for dim in Dimension:                       # deterministic order
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
    elif capped:                     # data problems outrank "small effect"
        tier = ClaimTier.SUGGESTIVE
    elif negligible:
        tier = ClaimTier.DETECTABLE_NOT_MEANINGFUL
    else:
        tier = ClaimTier.SUPPORTED

    return ClaimDecision(tier=tier, limiting_factors=factors,
                         permitted_phrasing_class=tier.value)
