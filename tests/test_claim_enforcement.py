"""Claim enforcement at the output boundary: an inconsistent claim cannot be constructed, so it cannot be
serialized over MCP. Envelope tests are pure; composite tests go through a real MCP client response."""

import copy
import sys
from datetime import date, timedelta

import pytest
from fastmcp import Client
from pydantic import ValidationError

import schemas
from qs_evidence import Dimension, DimensionResult, EvidenceProfile, Status, load_registry, resolve

START = date(2026, 3, 1)
REG = load_registry()
ALL_OK = {"sample": Status.ADEQUATE, "temporal": Status.ADEQUATE, "missingness": Status.ADEQUATE}
SUPPORTED = {"tier": "supported", "permitted_phrasing_class": "supported", "template": "Your data shows ..."}


def _profile(analysis: str, **statuses: Status) -> EvidenceProfile:
    dims = [
        DimensionResult(
            dimension=Dimension(name),
            status=status,
            reason_codes=[] if status == Status.ADEQUATE else [f"{name}_reason"],
        )
        for name, status in statuses.items()
    ]
    return EvidenceProfile(metric="m", analysis=analysis, dimensions=dims)


def _decision(profile: EvidenceProfile) -> dict:
    return resolve(profile, REG[profile.analysis]).to_mcp()


# ---- single-window envelope -----------------------------------------------------------------------------


def test_claim_evidence_accepts_the_resolver_decision_and_preserves_it():
    profile = _profile("baseline", **ALL_OK)
    claim = schemas.ClaimEvidence(
        evidence=schemas.Evidence.model_construct(),
        profile=profile,
        decision=schemas.ClaimDecisionOut(**_decision(profile)),
    )
    assert claim.decision.tier == "supported"


@pytest.mark.parametrize("override", [{"temporal": Status.WEAK}, {"sample": Status.BLOCKING}])
def test_claim_evidence_rejects_a_manually_strengthened_decision(override):
    profile = _profile("baseline", **{**ALL_OK, **override})
    strengthened = {**_decision(profile), **SUPPORTED}
    with pytest.raises(ValidationError, match="does not match"):
        schemas.ClaimEvidence(
            evidence=schemas.Evidence.model_construct(),
            profile=profile,
            decision=schemas.ClaimDecisionOut(**strengthened),
        )


def test_claim_evidence_rejects_a_decision_with_dropped_caveats():
    profile = _profile("baseline", **{**ALL_OK, "missingness": Status.CONCERN})
    stripped = {**_decision(profile), "must_state": []}
    with pytest.raises(ValidationError, match="does not match"):
        schemas.ClaimEvidence(
            evidence=schemas.Evidence.model_construct(),
            profile=profile,
            decision=schemas.ClaimDecisionOut(**stripped),
        )


# ---- two-source envelope --------------------------------------------------------------------------------


def test_comparative_claim_accepts_resolver_decision_and_rejects_strengthening_past_the_ceiling():
    profile = _profile("correlation", **ALL_OK)  # registry ceiling: suggestive / practical_not_evaluated
    honest = _decision(profile)
    assert honest["tier"] == "suggestive"
    ev = schemas.Evidence.model_construct()
    schemas.ClaimEvidenceComparative(
        evidence_a=ev, evidence_b=ev, profile=profile, decision=schemas.ClaimDecisionOut(**honest)
    )
    with pytest.raises(ValidationError, match="does not match"):
        schemas.ClaimEvidenceComparative(
            evidence_a=ev,
            evidence_b=ev,
            profile=profile,
            decision=schemas.ClaimDecisionOut(**{**honest, **SUPPORTED}),
        )


# ---- composite, on the wire -----------------------------------------------------------------------------


@pytest.fixture
def health_db(tmp_path, monkeypatch):
    monkeypatch.setenv("HEALTH_DB_PATH", str(tmp_path / "health.db"))
    for mod in ("server", "privacy", "tools.health", "tools.measurements"):
        sys.modules.pop(mod, None)
    import server

    return server


@pytest.fixture
async def client(health_db):
    async with Client(health_db.mcp) as c:
        yield c


async def _thin_history_explanation(client, health_db) -> dict:
    for i in range(20):
        health_db.log_daily_metric(date=(START + timedelta(days=i)).isoformat(), steps=8000 + (i % 5) * 400)
    target = (START + timedelta(days=19)).isoformat()
    return (await client.call_tool("explain_metric_change", {"metric": "steps", "date": target})).structured_content


async def test_explain_metric_change_response_revalidates_as_is(client, health_db):
    sc = await _thin_history_explanation(client, health_db)
    assert sc["overall_decision"]["tier"] == "insufficient"
    schemas.ExplainMetricChangeResult.model_validate(sc)


async def test_explain_metric_change_rejects_a_strengthened_overall_decision(client, health_db):
    sc = copy.deepcopy(await _thin_history_explanation(client, health_db))
    sc["overall_decision"] = {**sc["overall_decision"], **SUPPORTED}
    with pytest.raises(ValidationError, match="weakest-of-N"):
        schemas.ExplainMetricChangeResult.model_validate(sc)


async def test_explain_metric_change_rejects_an_overall_decision_that_drops_component_caveats(client, health_db):
    sc = copy.deepcopy(await _thin_history_explanation(client, health_db))
    assert sc["overall_decision"]["must_state"]
    sc["overall_decision"] = {**sc["overall_decision"], "must_state": []}
    with pytest.raises(ValidationError, match="weakest-of-N"):
        schemas.ExplainMetricChangeResult.model_validate(sc)


async def test_explain_metric_change_rejects_a_strengthened_component_claim(client, health_db):
    sc = copy.deepcopy(await _thin_history_explanation(client, health_db))
    sc["headline_claim"]["decision"] = {**sc["headline_claim"]["decision"], **SUPPORTED}
    with pytest.raises(ValidationError, match="does not match"):
        schemas.ExplainMetricChangeResult.model_validate(sc)
