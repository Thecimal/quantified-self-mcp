"""The registry describes reality: it lists exactly the dimensions the evaluators produce, no more, no fewer."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from qs_evidence import ClaimTier, Dimension, load_registry
from qs_evidence import assess as assess_mod
from qs_evidence.registry import MaxTier

START = date(2026, 5, 1)
REG = load_registry()
EFFECT_ANALYSES = ("trend", "window_comparison", "correlation", "anomaly")


def _pts(values, start=START):
    return [(start + timedelta(days=i), v) for i, v in enumerate(values)]


def _end(n, start=START):
    return start + timedelta(days=n - 1)


CALLS = {
    "trend": lambda v: assess_mod.assess_trend("m", _pts(v), START, _end(60)),
    "anomaly": lambda v: assess_mod.assess_anomaly("m", _pts(v), START, _end(60)),
    "baseline": lambda v: assess_mod.assess_baseline("m", _pts(v), START, _end(60)),
    "correlation": lambda v: assess_mod.assess_correlation("a", _pts(v), "b", _pts(v), START, _end(60)),
    "window_comparison": lambda v: assess_mod.assess_window_comparison(
        "m",
        _pts(v[30:], START + timedelta(days=30)),
        (START + timedelta(days=30), _end(60)),
        _pts(v[:30]),
        (START, _end(30)),
    ),
}


@pytest.mark.parametrize("analysis", sorted(REG))
def test_registry_lists_exactly_the_dimensions_its_evaluators_produce(analysis, monkeypatch):
    assert analysis in CALLS, f"registry analysis {analysis!r} has no assess_* entry point in this test"
    seen: dict[str, set[Dimension]] = {}
    real_finish = assess_mod._finish

    def spy(name, metric, effect, evaluated):
        seen[name] = set(evaluated)
        return real_finish(name, metric, effect, evaluated)

    monkeypatch.setattr(assess_mod, "_finish", spy)
    CALLS[analysis]([50.0 + (i % 5) for i in range(60)])

    listed = set(REG[analysis].dimensions)
    produced = seen[analysis]
    assert produced == listed, (
        f"{analysis}: registry lists without an evaluator {sorted(d.value for d in listed - produced)}; "
        f"evaluators produce without a registry entry {sorted(d.value for d in produced - listed)}"
    )


@pytest.mark.parametrize("analysis", EFFECT_ANALYSES)
def test_effect_analyses_without_a_practical_evaluator_declare_the_ceiling(analysis):
    spec = REG[analysis]
    if Dimension.PRACTICAL not in spec.dimensions:
        assert spec.max_tier == MaxTier(tier=ClaimTier.SUGGESTIVE, factor="practical_not_evaluated")
