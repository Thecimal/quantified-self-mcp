"""Evidence contracts. Nothing here knows how any statistic is computed."""
from __future__ import annotations
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field, model_validator


class Dimension(str, Enum):
    # Declaration order is the deterministic reporting order.
    SAMPLE = "sample"
    TEMPORAL = "temporal"
    ROBUSTNESS = "robustness"
    MISSINGNESS = "missingness"
    PRACTICAL = "practical"
    MEASUREMENT_VALIDITY = "measurement_validity"
    PROVENANCE = "provenance"


class Status(str, Enum):
    ADEQUATE = "adequate"
    NEGLIGIBLE = "negligible"      # practical only: real but below MDC / normal variation
    NOT_ASSESSED = "not_assessed"  # could not be evaluated; never defaults to adequate
    WEAK = "weak"
    CONCERN = "concern"
    BLOCKING = "blocking"


class ClaimTier(str, Enum):
    INSUFFICIENT = "insufficient"
    SUGGESTIVE = "suggestive"
    DETECTABLE_NOT_MEANINGFUL = "detectable_not_meaningful"
    SUPPORTED = "supported"


# Ordering used by tests (monotonicity). Higher = stronger claim allowed.
TIER_RANK = {ClaimTier.INSUFFICIENT: 0, ClaimTier.SUGGESTIVE: 1,
             ClaimTier.DETECTABLE_NOT_MEANINGFUL: 2, ClaimTier.SUPPORTED: 3}

PHRASING = {
    ClaimTier.INSUFFICIENT: "There isn't enough data to say.",
    ClaimTier.SUGGESTIVE: "Data hints at ..., but {limiting_factors}.",
    ClaimTier.DETECTABLE_NOT_MEANINGFUL:
        "A small change is visible but within your normal variation.",
    ClaimTier.SUPPORTED: "Your data shows ...",
}


class DimensionResult(BaseModel):
    dimension: Dimension
    status: Status
    reason_codes: list[str] = Field(default_factory=list)
    # Diagnostic quantities (e.g. lag-1 n_eff) set can_block=False: a "blocking"
    # status from them is demoted to "weak". Only observed quantities may block.
    can_block: bool = True
    details: dict[str, Any] = Field(default_factory=dict)  # opaque to the resolver

    @model_validator(mode="after")
    def _reasons_required(self):
        if self.status != Status.ADEQUATE and not self.reason_codes:
            raise ValueError(
                f"{self.dimension.value}: status={self.status.value} requires reason_codes")
        if self.status == Status.NEGLIGIBLE and self.dimension != Dimension.PRACTICAL:
            raise ValueError("negligible is only valid for the practical dimension")
        return self


class EvidenceProfile(BaseModel):
    metric: str
    analysis: str
    effect: dict[str, Any] = Field(default_factory=dict)
    dimensions: list[DimensionResult]

    def get(self, dim: Dimension) -> DimensionResult | None:
        for d in self.dimensions:
            if d.dimension == dim:
                return d
        return None


class ClaimDecision(BaseModel):
    tier: ClaimTier
    limiting_factors: list[str]          # becomes must_state
    permitted_phrasing_class: str

    def to_mcp(self) -> dict[str, Any]:
        return {"tier": self.tier.value,
                "permitted_phrasing_class": self.permitted_phrasing_class,
                "must_state": self.limiting_factors,
                "template": PHRASING[self.tier]}
