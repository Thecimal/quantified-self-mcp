import pytest
from pydantic import BaseModel, ValidationError

import schemas
import server


def _all_subclasses(cls):
    for sub in cls.__subclasses__():
        yield sub
        yield from _all_subclasses(sub)


def _claim_fields(model):
    return {n: f for n, f in model.model_fields.items() if n == "claim" or n.endswith("_claim")}


LEGACY_CLAIM_FIELDS = {"evidence_profile", "claim_decision", "trend_evidence_profile", "trend_claim_decision"}


# Add a model here only with a written reason.
# Descriptive results that carry coverage evidence but make no analytical claim.
NOT_CLAIM_BEARING: set[str] = {
    "GetMetricHistoryResult",  # raw history, no claim
    "ClaimEvidence",  # the canonical claim envelope itself, not a result that carries one
}

EVIDENCE_MODELS = [
    m
    for m in _all_subclasses(BaseModel)
    if m.__module__ in (server.__name__, schemas.__name__) and _claim_fields(m) and m.__name__ not in NOT_CLAIM_BEARING
]

# Result models now live in schemas.py, imported into server.py; without checking
# both modules above, EVIDENCE_MODELS silently collects zero classes and every
# parametrized test below passes vacuously (pytest reports it as skipped, not
# failed). Assert the exact set of models discovered so that regression is loud.
_EXPECTED_EVIDENCE_MODELS = {
    "GetBaselineResult",
    "DetectAnomaliesResult",
    "CalculateTrendResult",
    "ComparePeriodsResult",
    "CorrelationResult",
    "ChangeNote",
    "ExplainMetricChangeResult",
}


def test_evidence_models_discovery_is_not_vacuous():
    discovered = {m.__name__ for m in EVIDENCE_MODELS}
    assert discovered == _EXPECTED_EVIDENCE_MODELS


@pytest.mark.parametrize("model", EVIDENCE_MODELS, ids=lambda m: m.__name__)
def test_evidence_bearing_models_require_claim_fields(model):
    claim = _claim_fields(model)
    assert claim, f"{model.__name__} has evidence but no claim fields"
    assert all(f.is_required() for f in claim.values())


def test_no_model_declares_a_legacy_flat_claim_field():
    models = [m for m in _all_subclasses(BaseModel) if m.__module__ in (server.__name__, schemas.__name__)]
    assert len(models) > len(_EXPECTED_EVIDENCE_MODELS)  # scanning the real model set, not an empty list
    offenders = {m.__name__: sorted(LEGACY_CLAIM_FIELDS & set(m.model_fields)) for m in models}
    assert not {name: fields for name, fields in offenders.items() if fields}


def test_legacy_claim_mixin_and_helper_are_gone():
    for module in (schemas, server):
        assert not hasattr(module, "ClaimFields"), module.__name__
    assert not hasattr(server, "_claim_fields")
    assert not hasattr(server, "_migrated_claim_fields")


def test_explain_metric_change_requires_trend_claim_fields():
    fields = server.ExplainMetricChangeResult.model_fields
    assert fields["headline_claim"].is_required()
    assert fields["headline_claim"].annotation is server.ClaimEvidence
    assert fields["trend_claim"].is_required()
    assert fields["trend_claim"].annotation is server.ClaimEvidence


def test_explain_metric_change_requires_overall_decision():
    field = server.ExplainMetricChangeResult.model_fields["overall_decision"]
    assert field.is_required() and field.annotation is server.ClaimDecisionOut


def test_trend_result_without_claim_fields_is_rejected():
    with pytest.raises(ValidationError):
        server.CalculateTrendResult(
            metric="steps",
            range=server.DateRange(start_date="2026-01-01", end_date="2026-01-30"),
            trend=server.TrendStats(direction="increasing", slope_per_day=10.0, r_squared=0.8, n=30, span_days=29),
            evidence=server.Evidence(
                requested_start="2026-01-01",
                requested_end="2026-01-30",
                expected_days=30,
                observed_days=30,
                coverage_ratio=1.0,
                missing_days=0,
                measurement_count=30,
                gaps=[],
                recent_gap_days=0,
                confidence="high",
            ),
        )


def test_baseline_result_requires_the_canonical_claim_envelope():
    field = server.GetBaselineResult.model_fields["claim"]
    assert field.is_required() and field.annotation is server.ClaimEvidence
    assert server.GetBaselineResult.model_fields["evidence"].deprecated
