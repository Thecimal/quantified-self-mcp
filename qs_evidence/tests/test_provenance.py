"""Provenance dimension: source conflicts and source changes reach the ClaimDecision, and a missing read can
only lower it.

The ClaimDecision used to be blind to provenance (data_health reported MIXED_SOURCES, the decision still said
"supported" with an empty must_state). These tests pin the contract at the evaluator and at every assess_* entry
point; tests/test_evidence_contract_mcp.py pins it on the wire.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from qs_evidence import ClaimTier, Dimension, SourceSummary, Status, load_registry
from qs_evidence import assess as assess_mod
from qs_evidence.models import TIER_RANK
from qs_evidence.provenance import (
    SOURCE_CHANGED_BETWEEN,
    SOURCE_CHANGED_WITHIN,
    UNAVAILABLE,
    UNRANKED_CONFLICT,
    evaluate_provenance,
)

START = date(2026, 5, 1)
REG = load_registry()
CLEAN = SourceSummary(("src",), 0)
CONFLICT = SourceSummary(("a", "b"), 12)
CHANGED = SourceSummary(("a", "b"), 0)


def _pts(values, start=START):
    return [(start + timedelta(days=i), v) for i, v in enumerate(values)]


def _end(n, start=START):
    return start + timedelta(days=n - 1)


WAVE = [50.0 + (i % 5) for i in range(60)]


def _call(analysis: str, prov_a, prov_b=None):
    """Assessment of otherwise-clean data with the given provenance for its window(s)."""
    if analysis == "baseline":
        return assess_mod.assess_baseline("m", _pts(WAVE), START, _end(60), provenance=prov_a)
    if analysis == "trend":
        return assess_mod.assess_trend("m", _pts(WAVE), START, _end(60), provenance=prov_a)
    if analysis == "anomaly":
        return assess_mod.assess_anomaly("m", _pts(WAVE), START, _end(60), provenance=prov_a)
    if analysis == "correlation":
        return assess_mod.assess_correlation(
            "a", _pts(WAVE), "b", _pts(WAVE), START, _end(60), provenance_a=prov_a, provenance_b=prov_b or prov_a
        )
    assert analysis == "window_comparison"
    mid = START + timedelta(days=30)
    return assess_mod.assess_window_comparison(
        "m",
        _pts(WAVE[30:], mid),
        (mid, _end(60)),
        _pts(WAVE[:30]),
        (START, _end(30)),
        provenance_a=prov_a,
        provenance_b=prov_b or prov_a,
    )


def _prov(a) -> object:
    return next(d for d in a.profile.dimensions if d.dimension == Dimension.PROVENANCE)


# ---- evaluator ------------------------------------------------------------------------------------------


def test_one_consistent_source_without_tie_breaks_is_adequate():
    r = evaluate_provenance({"window": CLEAN})
    assert r.status == Status.ADEQUATE and r.reason_codes == []


def test_an_unranked_tie_break_weakens_with_its_reason():
    r = evaluate_provenance({"window": SourceSummary(("a",), 1)})
    assert r.status == Status.WEAK and r.reason_codes == [UNRANKED_CONFLICT]


def test_a_source_change_inside_the_window_weakens_with_its_reason():
    r = evaluate_provenance({"window": CHANGED})
    assert r.status == Status.WEAK and r.reason_codes == [SOURCE_CHANGED_WITHIN]


def test_an_unreadable_summary_is_not_assessed_never_adequate():
    r = evaluate_provenance({"window": None})
    assert r.status == Status.NOT_ASSESSED and r.reason_codes == [UNAVAILABLE]
    assert evaluate_provenance({}).status == Status.NOT_ASSESSED


def test_a_problem_in_one_window_outranks_a_gap_in_another():
    r = evaluate_provenance({"period_a": CONFLICT, "period_b": None})
    assert r.status == Status.WEAK and UNRANKED_CONFLICT in r.reason_codes


def test_different_sources_between_periods_of_one_metric_are_a_reason_but_between_two_metrics_are_not():
    a, b = SourceSummary(("watch",), 0), SourceSummary(("phone",), 0)
    same = evaluate_provenance({"period_a": a, "period_b": b}, same_metric=True)
    assert same.status == Status.WEAK and same.reason_codes == [SOURCE_CHANGED_BETWEEN]
    different = evaluate_provenance({"metric_a": a, "metric_b": b}, same_metric=False)
    assert different.status == Status.ADEQUATE


def test_empty_windows_do_not_count_as_a_source_change():
    r = evaluate_provenance({"period_a": SourceSummary((), 0), "period_b": CLEAN}, same_metric=True)
    assert r.status == Status.ADEQUATE


# ---- every analysis: provenance reaches the decision ----------------------------------------------------

ANALYSES = sorted(REG)


@pytest.mark.parametrize("analysis", ANALYSES)
def test_every_registry_analysis_lists_provenance_and_caps_on_not_assessed(analysis):
    assert REG[analysis].dimensions[Dimension.PROVENANCE].on_not_assessed == "cap"


@pytest.mark.parametrize("analysis", ANALYSES)
def test_missing_provenance_never_silently_becomes_a_supported_claim(analysis):
    a = _call(analysis, None)
    assert _prov(a).status == Status.NOT_ASSESSED
    assert TIER_RANK[a.decision.tier] <= TIER_RANK[ClaimTier.SUGGESTIVE]
    assert "provenance_not_assessed" in a.decision.limiting_factors


@pytest.mark.parametrize("analysis", ANALYSES)
@pytest.mark.parametrize(("summary", "code"), [(CONFLICT, UNRANKED_CONFLICT), (CHANGED, SOURCE_CHANGED_WITHIN)])
def test_degraded_provenance_downgrades_otherwise_clean_data(analysis, summary, code):
    clean = _call(analysis, CLEAN)
    degraded = _call(analysis, summary)
    assert code in degraded.decision.limiting_factors
    assert code not in clean.decision.limiting_factors
    assert TIER_RANK[degraded.decision.tier] <= TIER_RANK[clean.decision.tier]
    assert degraded.decision.tier != ClaimTier.SUPPORTED


def test_baseline_is_the_only_analysis_that_can_still_be_supported_and_only_with_clean_provenance():
    assert _call("baseline", CLEAN).decision.tier == ClaimTier.SUPPORTED
    for bad in (None, CONFLICT, CHANGED):
        assert _call("baseline", bad).decision.tier == ClaimTier.SUGGESTIVE


def test_period_comparison_names_a_source_change_between_the_periods():
    a = _call("window_comparison", SourceSummary(("watch",), 0), SourceSummary(("phone",), 0))
    assert SOURCE_CHANGED_BETWEEN in a.decision.limiting_factors


def test_provenance_cannot_rescue_insufficient_data():
    a = assess_mod.assess_baseline("m", _pts(WAVE[:5]), START, _end(5), provenance=CLEAN)
    assert a.decision.tier == ClaimTier.INSUFFICIENT


def test_provenance_does_not_change_the_statistical_effect():
    effect = {"mean": 52.0, "n": 60}
    a = assess_mod.assess_baseline("m", _pts(WAVE), START, _end(60), effect=effect, provenance=CONFLICT)
    assert a.profile.effect == effect
