"""The registry's max_tier ceiling: explicit, greppable, and never a substitute for a real evaluator."""

from __future__ import annotations

from qs_evidence import (
    AnalysisSpec,
    ClaimTier,
    Dimension,
    DimensionResult,
    EvidenceProfile,
    Status,
    load_registry,
    resolve,
)
from qs_evidence.registry import DimensionPolicy, MaxTier

CEILING = MaxTier(tier=ClaimTier.SUGGESTIVE, factor="practical_not_evaluated")


def _spec(max_tier=None):
    return AnalysisSpec(
        name="t",
        dimensions={d: DimensionPolicy(on_not_assessed="cap") for d in (Dimension.SAMPLE, Dimension.TEMPORAL)},
        max_tier=max_tier,
    )


def _profile(sample=None, temporal=None):
    ok = DimensionResult
    return EvidenceProfile(
        metric="m",
        analysis="t",
        dimensions=[
            sample or ok(dimension=Dimension.SAMPLE, status=Status.ADEQUATE),
            temporal or ok(dimension=Dimension.TEMPORAL, status=Status.ADEQUATE),
        ],
    )


def test_without_a_ceiling_a_clean_claim_is_supported():
    d = resolve(_profile(), _spec())
    assert d.tier == ClaimTier.SUPPORTED and d.limiting_factors == []


def test_a_clean_claim_is_held_at_the_ceiling_and_says_why():
    d = resolve(_profile(), _spec(CEILING))
    assert d.tier == ClaimTier.SUGGESTIVE
    assert d.limiting_factors == ["practical_not_evaluated"]
    assert d.permitted_phrasing_class == "suggestive"


def test_the_ceiling_never_hides_real_data_problems():
    weak = DimensionResult(dimension=Dimension.TEMPORAL, status=Status.WEAK, reason_codes=["recent_window_gap"])
    d = resolve(_profile(temporal=weak), _spec(CEILING))
    assert d.tier == ClaimTier.SUGGESTIVE
    assert d.limiting_factors == ["recent_window_gap", "practical_not_evaluated"]


def test_the_ceiling_never_raises_a_weaker_tier():
    blocked = DimensionResult(dimension=Dimension.SAMPLE, status=Status.BLOCKING, reason_codes=["n_below_minimum"])
    d = resolve(_profile(sample=blocked), _spec(CEILING))
    assert d.tier == ClaimTier.INSUFFICIENT
    assert d.limiting_factors == ["n_below_minimum"]


def test_registry_loads_the_ceiling_and_baseline_has_none():
    reg = load_registry()
    assert reg["trend"].max_tier == CEILING
    assert reg["baseline"].max_tier is None
