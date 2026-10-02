"""P0 contract tests for the optional Jev integration.

These exist to prove the one invariant that matters before any network code
is written: nothing Jev returns can mutate, strengthen, or substitute for a
ClaimEvidence. See docstrings on integrations.jev for the scope of this
patch (contract only, no client, no network)."""

import copy
import dataclasses
import inspect
import socket

import pytest

import schemas
from integrations.jev import ALLOWED_ANOMALY_FIELDS, JevInterpretation, build_anomaly_question_input
from qs_evidence import Dimension, DimensionResult, EvidenceProfile, Status, load_registry, resolve

REG = load_registry()


def _profile(**statuses: Status) -> EvidenceProfile:
    dims = [
        DimensionResult(
            dimension=Dimension(name),
            status=status,
            reason_codes=[] if status == Status.ADEQUATE else [f"{name}_reason"],
        )
        for name, status in statuses.items()
    ]
    return EvidenceProfile(metric="resting_heart_rate", analysis="anomaly", dimensions=dims)


def _claim(**statuses: Status) -> schemas.ClaimEvidence:
    profile = _profile(**statuses)
    decision = resolve(profile, REG[profile.analysis]).to_mcp()
    return schemas.ClaimEvidence(
        evidence=schemas.Evidence.model_construct(expected_days=30, observed_days=27, coverage_ratio=0.9),
        profile=profile,
        decision=schemas.ClaimDecisionOut(**decision),
    )


def _anomaly_result(claim: schemas.ClaimEvidence) -> schemas.DetectAnomaliesResult:
    return schemas.DetectAnomaliesResult(
        metric="resting_heart_rate",
        range=schemas.DateRange(start_date="2026-08-01", end_date="2026-08-30"),
        threshold=3.5,
        anomalies=[],
        claim=claim,
        evidence=claim.evidence,
    )


# ---- JevInterpretation is frozen and structurally incapable of carrying a claim --------------------------


def test_jev_interpretation_is_frozen():
    interp = JevInterpretation(
        category="large_increase", probabilities={"large_increase": 0.8}, confidence=0.8, model="jev-1.13"
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        interp.category = "something_else"


def test_jev_interpretation_shares_no_field_with_a_claim_decision_or_envelope():
    jev_fields = {f.name for f in dataclasses.fields(JevInterpretation)}
    assert not jev_fields & set(schemas.ClaimDecisionOut.model_fields)
    assert not jev_fields & set(schemas.ClaimEvidence.model_fields)


# ---- building the sanitized input never mutates the source result, and never exceeds the allowlist -------


def test_build_anomaly_question_input_does_not_mutate_the_result():
    claim = _claim(sample=Status.ADEQUATE, temporal=Status.ADEQUATE)
    result = _anomaly_result(claim)
    before = copy.deepcopy(result)
    build_anomaly_question_input(result, change_percent=18.5)
    assert result == before


def test_build_anomaly_question_input_matches_the_allowlist_exactly():
    claim = _claim(sample=Status.ADEQUATE, temporal=Status.ADEQUATE)
    result = _anomaly_result(claim)
    payload = build_anomaly_question_input(result, change_percent=18.5)
    assert set(payload) == set(ALLOWED_ANOMALY_FIELDS)


def test_build_anomaly_question_input_excludes_coverage_detail_not_on_the_allowlist():
    claim = _claim(sample=Status.ADEQUATE, temporal=Status.ADEQUATE)
    result = _anomaly_result(claim)
    payload = build_anomaly_question_input(result, change_percent=18.5)
    # gaps/freshness_days/measurement_count/recent_gap_days are coverage detail the allowlist
    # deliberately omits; a leak of any of these would be a privacy regression, not a feature.
    for field in ("gaps", "freshness_days", "measurement_count", "recent_gap_days"):
        assert field not in payload


def test_build_anomaly_question_input_rejects_a_payload_outside_the_allowlist(monkeypatch):
    import integrations.jev.adapter as adapter_module

    monkeypatch.setattr(adapter_module, "ALLOWED_ANOMALY_FIELDS", frozenset({"metric"}))
    claim = _claim(sample=Status.ADEQUATE, temporal=Status.ADEQUATE)
    result = _anomaly_result(claim)
    with pytest.raises(ValueError, match="outside the allowlist"):
        build_anomaly_question_input(result, change_percent=18.5)


# ---- an insufficient claim has no path to become supported via a Jev interpretation ----------------------


def test_insufficient_claim_tier_is_unreachable_by_jev_confidence():
    claim = _claim(sample=Status.BLOCKING, temporal=Status.ADEQUATE)
    assert claim.decision.tier == "insufficient"
    result = _anomaly_result(claim)

    # A maximally confident Jev interpretation of the same result.
    JevInterpretation(
        category="large_increase", probabilities={"large_increase": 0.99}, confidence=0.99, model="jev-1.13"
    )

    # The only sanctioned path out of this package is claim -> sanitized payload.
    # Prove there is no function going the other way: nothing here accepts a
    # JevInterpretation and returns (or accepts and mutates) a claim/decision.
    import integrations.jev as jev_pkg

    for name in jev_pkg.__all__:
        obj = getattr(jev_pkg, name)
        if not inspect.isfunction(obj):
            continue
        params = inspect.signature(obj).parameters
        sig = inspect.signature(obj)
        accepts_jev_interpretation = any(
            p.annotation is JevInterpretation or p.annotation == "JevInterpretation" for p in params.values()
        )
        returns_claim_type = sig.return_annotation in (
            schemas.ClaimEvidence,
            schemas.ClaimEvidenceComparative,
            schemas.ClaimDecisionOut,
        )
        assert not accepts_jev_interpretation, f"{name} must not accept a JevInterpretation"
        assert not returns_claim_type, f"{name} must not produce a claim/decision"

    # Untouched by any of the above.
    assert claim.decision.tier == "insufficient"
    assert result.claim.decision.tier == "insufficient"


# ---- no network path exists yet -----------------------------------------------------------------------


def test_jev_package_has_no_http_client_dependency():
    assert not hasattr(__import__("integrations.jev", fromlist=["adapter"]).adapter, "client")
    import sys

    # The P0 patch must not have pulled in an HTTP library as a side effect of import.
    assert "httpx" not in sys.modules
    assert "requests" not in sys.modules


def test_build_anomaly_question_input_opens_no_sockets(monkeypatch):
    def _blocked(*a, **kw):
        raise AssertionError("integrations.jev must not open sockets in the P0 contract")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    claim = _claim(sample=Status.ADEQUATE, temporal=Status.ADEQUATE)
    result = _anomaly_result(claim)
    build_anomaly_question_input(result, change_percent=18.5)
