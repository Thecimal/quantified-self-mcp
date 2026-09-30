"""validate_claim_decision: a decision is only accepted if the canonical resolver derives it from its profile."""

from datetime import date, timedelta

import pytest

from qs_evidence import (
    ClaimTier,
    Dimension,
    DimensionResult,
    EvidenceProfile,
    Status,
    assess_trend,
    combine_decisions,
    load_registry,
    resolve,
    validate_claim_decision,
)
from qs_evidence.registry import AnalysisSpec, DimensionPolicy

REG = load_registry()
S = Status
ALL_OK = {"sample": S.ADEQUATE, "temporal": S.ADEQUATE, "missingness": S.ADEQUATE}


def _profile(analysis: str, **statuses: Status) -> EvidenceProfile:
    dims = [
        DimensionResult(
            dimension=Dimension(name),
            status=status,
            reason_codes=[] if status == S.ADEQUATE else [f"{name}_reason"],
        )
        for name, status in statuses.items()
    ]
    return EvidenceProfile(metric="m", analysis=analysis, dimensions=dims)


def _decide(profile: EvidenceProfile) -> dict:
    return resolve(profile, REG[profile.analysis]).to_mcp()


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({}, ClaimTier.SUPPORTED),
        ({"temporal": S.WEAK}, ClaimTier.SUGGESTIVE),
        ({"missingness": S.CONCERN}, ClaimTier.SUGGESTIVE),
        ({"sample": S.BLOCKING}, ClaimTier.INSUFFICIENT),
        ({"missingness": S.BLOCKING}, ClaimTier.INSUFFICIENT),
    ],
)
def test_valid_decision_for_each_tier_is_accepted_and_reflects_the_dimension(override, expected):
    profile = _profile("baseline", **{**ALL_OK, **override})
    decision = _decide(profile)
    assert decision["tier"] == expected.value
    for name in override:
        assert f"{name}_reason" in decision["must_state"]
    validate_claim_decision(profile, decision)


@pytest.mark.parametrize("override", [{"temporal": S.WEAK}, {"missingness": S.CONCERN}, {"sample": S.BLOCKING}])
def test_strengthened_decision_is_rejected(override):
    profile = _profile("baseline", **{**ALL_OK, **override})
    honest = _decide(profile)
    with pytest.raises(ValueError, match="does not match"):
        validate_claim_decision(profile, {**honest, "tier": "supported", "permitted_phrasing_class": "supported"})


def test_dropped_must_state_and_altered_template_are_rejected():
    profile = _profile("baseline", **{**ALL_OK, "temporal": S.WEAK})
    honest = _decide(profile)
    assert honest["must_state"]
    with pytest.raises(ValueError, match="does not match"):
        validate_claim_decision(profile, {**honest, "must_state": []})
    with pytest.raises(ValueError, match="does not match"):
        validate_claim_decision(profile, {**honest, "template": "Your data shows ..."})


def test_missing_required_dimension_is_conservative_and_cannot_be_claimed_supported():
    profile = _profile("baseline", sample=S.ADEQUATE, temporal=S.ADEQUATE)  # missingness absent
    decision = _decide(profile)
    assert decision["tier"] == "suggestive"
    assert "missingness_not_assessed" in decision["must_state"]
    validate_claim_decision(profile, decision)
    with pytest.raises(ValueError, match="does not match"):
        validate_claim_decision(profile, {**decision, "tier": "supported", "permitted_phrasing_class": "supported"})


def test_declared_ceiling_is_enforced_by_the_validator():
    profile = _profile("correlation", **ALL_OK)
    decision = _decide(profile)
    assert decision["tier"] == "suggestive" and "practical_not_evaluated" in decision["must_state"]
    validate_claim_decision(profile, decision)
    with pytest.raises(ValueError, match="does not match"):
        validate_claim_decision(profile, {**decision, "tier": "supported", "permitted_phrasing_class": "supported"})


def test_unknown_analysis_is_rejected_not_passed():
    profile = _profile("baseline", **ALL_OK).model_copy(update={"analysis": "no_such_analysis"})
    with pytest.raises(ValueError, match="unknown analysis"):
        validate_claim_decision(profile, _decide(_profile("baseline", **ALL_OK)))


def test_detectable_not_meaningful_tier_against_a_supplied_spec():
    spec = AnalysisSpec(
        name="x",
        dimensions={
            Dimension.SAMPLE: DimensionPolicy(on_not_assessed="cap"),
            Dimension.PRACTICAL: DimensionPolicy(on_not_assessed="cap"),
        },
    )
    profile = EvidenceProfile(
        metric="m",
        analysis="x",
        dimensions=[
            DimensionResult(dimension=Dimension.SAMPLE, status=S.ADEQUATE),
            DimensionResult(dimension=Dimension.PRACTICAL, status=S.NEGLIGIBLE, reason_codes=["below_mdc"]),
        ],
    )
    decision = resolve(profile, spec).to_mcp()
    assert decision["tier"] == "detectable_not_meaningful"
    validate_claim_decision(profile, decision, spec=spec)
    with pytest.raises(ValueError, match="does not match"):
        validate_claim_decision(profile, {**decision, "tier": "supported"}, spec=spec)


def test_tiny_trend_sample_assessed_end_to_end_is_insufficient_and_valid():
    start = date(2026, 3, 1)
    points = [(start + timedelta(days=i), 1000.0 + i) for i in range(5)]
    a = assess_trend("steps", points, start, start + timedelta(days=4))
    assert a.decision.tier == ClaimTier.INSUFFICIENT
    validate_claim_decision(a.profile, a.decision.to_mcp())


def test_composite_with_no_components_is_an_error_not_a_pass():
    with pytest.raises(ValueError, match="at least one"):
        combine_decisions([])
