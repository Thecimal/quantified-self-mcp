import pytest
from pydantic import BaseModel, ValidationError

import server


def _all_subclasses(cls):
    for sub in cls.__subclasses__():
        yield sub
        yield from _all_subclasses(sub)


def _claim_fields(model):
    return {
        n: f
        for n, f in model.model_fields.items()
        if n.endswith(("evidence_profile", "claim_decision"))
    }


# Add a model here only with a written reason.
# Descriptive results that carry coverage evidence but make no analytical claim.
NOT_CLAIM_BEARING: set[str] = {
    "GetMetricHistoryResult",  # raw history, no claim
    "GetBaselineResult",  # descriptive baseline stats, no claim
}

EVIDENCE_MODELS = [
    m
    for m in _all_subclasses(BaseModel)
    if m.__module__ == server.__name__
    and any(n.endswith("evidence") for n in m.model_fields)
    and m.__name__ not in NOT_CLAIM_BEARING
]


@pytest.mark.parametrize("model", EVIDENCE_MODELS, ids=lambda m: m.__name__)
def test_evidence_bearing_models_require_claim_fields(model):
    claim = _claim_fields(model)
    assert claim, f"{model.__name__} has evidence but no claim fields"
    assert all(f.is_required() for f in claim.values())


@pytest.mark.parametrize(
    "model", list(_all_subclasses(server.ClaimFields)), ids=lambda m: m.__name__
)
def test_claim_fields_subclasses_are_required(model):
    assert all(f.is_required() for f in _claim_fields(model).values())


def test_explain_metric_change_requires_trend_claim_fields():
    fields = server.ExplainMetricChangeResult.model_fields
    assert fields["trend_evidence_profile"].is_required()
    assert fields["trend_claim_decision"].is_required()


def test_trend_result_without_claim_fields_is_rejected():
    with pytest.raises(ValidationError):
        server.CalculateTrendResult(
            metric="steps",
            range=server.DateRange(start_date="2026-01-01", end_date="2026-01-30"),
            trend=server.TrendStats(
                direction="increasing", slope_per_day=10.0, r_squared=0.8, n=30, span_days=29
            ),
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
