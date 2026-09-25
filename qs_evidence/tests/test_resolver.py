import inspect
import itertools
import random

import pytest

from qs_evidence import (
    ClaimDecision,
    ClaimTier,
    DimensionResult,
    EvidenceProfile,
    combine_decisions,
    load_registry,
    resolve,
)
from qs_evidence import Dimension as D
from qs_evidence import Status as S
from qs_evidence.models import TIER_RANK

REG = load_registry()
SPEC = REG["window_comparison"]


def dr(dim, status=S.ADEQUATE, *codes, can_block=True):
    return DimensionResult(dimension=dim, status=status, reason_codes=list(codes), can_block=can_block)


def profile(**overrides):
    dims = {d: dr(d) for d in D}
    dims.update({D(k): v for k, v in overrides.items()})
    return EvidenceProfile(
        metric="hrv_rmssd", analysis="window_comparison", effect={"change_pct": -24}, dimensions=list(dims.values())
    )


FIXTURES = [
    ("clean_60d", {}, ClaimTier.SUPPORTED, []),
    (
        "gappy_recent",
        {
            "temporal": dr(D.TEMPORAL, S.WEAK, "recent_window_gap"),
            "missingness": dr(D.MISSINGNESS, S.WEAK, "coverage_below_threshold"),
        },
        ClaimTier.SUGGESTIVE,
        ["recent_window_gap", "coverage_below_threshold"],
    ),
    (
        "outlier_driven",
        {"robustness": dr(D.ROBUSTNESS, S.WEAK, "top3_influence_high")},
        ClaimTier.SUGGESTIVE,
        ["top3_influence_high"],
    ),
    (
        "sign_flip",
        {"robustness": dr(D.ROBUSTNESS, S.BLOCKING, "loo_sign_flip")},
        ClaimTier.INSUFFICIENT,
        ["loo_sign_flip"],
    ),
    ("tiny_n", {"sample": dr(D.SAMPLE, S.BLOCKING, "n_below_minimum")}, ClaimTier.INSUFFICIENT, ["n_below_minimum"]),
    (
        "within_noise",
        {"practical": dr(D.PRACTICAL, S.NEGLIGIBLE, "below_mdc")},
        ClaimTier.DETECTABLE_NOT_MEANINGFUL,
        ["below_mdc"],
    ),
    (
        "device_switch",
        {"provenance": dr(D.PROVENANCE, S.CONCERN, "shift_coincides_with_source_change")},
        ClaimTier.SUGGESTIVE,
        ["shift_coincides_with_source_change"],
    ),
    (
        "mnar_sleep",
        {"missingness": dr(D.MISSINGNESS, S.CONCERN, "missing_correlates_with_low_sleep")},
        ClaimTier.SUGGESTIVE,
        ["missing_correlates_with_low_sleep"],
    ),
]


@pytest.mark.parametrize("name,ov,tier,factors", FIXTURES, ids=[f[0] for f in FIXTURES])
def test_same_effect_different_evidence(name, ov, tier, factors):
    d = resolve(profile(**ov), SPEC)
    assert d.tier == tier
    assert d.limiting_factors == factors
    assert d.permitted_phrasing_class == tier.value


def test_diagnostic_cannot_block():
    p = profile(sample=dr(D.SAMPLE, S.BLOCKING, "n_eff_low", can_block=False))
    assert resolve(p, SPEC).tier == ClaimTier.SUGGESTIVE


def test_not_assessed_caps_by_default_and_is_visible():
    d = resolve(profile(provenance=dr(D.PROVENANCE, S.NOT_ASSESSED, "no_device_metadata")), SPEC)
    assert d.tier == ClaimTier.SUGGESTIVE and "provenance_not_assessed" in d.limiting_factors


def test_not_assessed_tolerated_when_registry_says_so():
    p = profile(measurement_validity=dr(D.MEASUREMENT_VALIDITY, S.NOT_ASSESSED, "no_error_model"))
    assert resolve(p, SPEC).tier == ClaimTier.SUPPORTED


def test_missing_required_dimension_is_not_adequate():
    p = profile()
    p.dimensions = [x for x in p.dimensions if x.dimension != D.SAMPLE]
    d = resolve(p, SPEC)
    assert d.tier == ClaimTier.SUGGESTIVE and d.limiting_factors == ["sample_not_assessed"]


def test_data_problem_outranks_small_effect():
    p = profile(
        practical=dr(D.PRACTICAL, S.NEGLIGIBLE, "below_mdc"), temporal=dr(D.TEMPORAL, S.WEAK, "recent_window_gap")
    )
    assert resolve(p, SPEC).tier == ClaimTier.SUGGESTIVE


def test_reason_codes_required_when_not_adequate():
    with pytest.raises(ValueError):
        DimensionResult(dimension=D.SAMPLE, status=S.WEAK)


def test_inapplicable_dimension_ignored():
    p = profile(missingness=dr(D.MISSINGNESS, S.BLOCKING, "x"))
    assert resolve(p, REG["anomaly"]).tier == ClaimTier.SUPPORTED


# ---- monotonicity: degrading one dimension one step never raises the tier ----
def chain(dim):
    base = (
        [S.ADEQUATE] + ([S.NEGLIGIBLE] if dim == D.PRACTICAL else []) + [S.NOT_ASSESSED, S.WEAK, S.CONCERN, S.BLOCKING]
    )
    return base


def build(statuses):
    return profile(**{d.value: dr(d, s, "r") if s != S.ADEQUATE else dr(d) for d, s in statuses.items()})


@pytest.mark.parametrize("analysis", list(REG))
def test_monotonicity(analysis):
    spec, rng = REG[analysis], random.Random(7)
    dims = list(spec.dimensions)
    for _ in range(500):
        st = {d: rng.choice(chain(d)) for d in dims}
        before = TIER_RANK[resolve(_p(st, analysis), spec).tier]
        d = rng.choice(dims)
        c = chain(d)
        i = c.index(st[d])
        if i + 1 < len(c):
            st2 = {**st, d: c[i + 1]}
            after = TIER_RANK[resolve(_p(st2, analysis), spec).tier]
            assert after <= before, (st, d, c[i + 1])


def _p(statuses, analysis):
    ds = [dr(d, s, "r") if s != S.ADEQUATE else dr(d) for d, s in statuses.items()]
    return EvidenceProfile(metric="m", analysis=analysis, dimensions=ds)


def test_mcp_contract():
    out = resolve(profile(**FIXTURES[6][1]), SPEC).to_mcp()
    assert out["tier"] == "suggestive" and out["must_state"] == ["shift_coincides_with_source_change"]


# ---------------------------------------------------------------------------
# combine_decisions: weakest-of-N composite primitive
# ---------------------------------------------------------------------------


def cd(tier, *factors):
    return ClaimDecision(tier=tier, limiting_factors=list(factors), permitted_phrasing_class=tier.value)


def test_combine_decisions_requires_at_least_one():
    with pytest.raises(ValueError):
        combine_decisions([])


def test_combine_decisions_single_is_passthrough_tier():
    d = cd(ClaimTier.SUGGESTIVE, "gappy_recent")
    out = combine_decisions([d])
    assert out.tier == ClaimTier.SUGGESTIVE
    assert out.limiting_factors == ["gappy_recent"]


def test_combine_decisions_three_component_weakest_wins():
    # baseline -> supported, trend -> suggestive, correlation -> insufficient
    baseline = cd(ClaimTier.SUPPORTED)
    trend = cd(ClaimTier.SUGGESTIVE, "recent_window_gap")
    correlation = cd(ClaimTier.INSUFFICIENT, "n_below_minimum")
    out = combine_decisions([baseline, trend, correlation])
    assert out.tier == ClaimTier.INSUFFICIENT
    assert out.limiting_factors == ["n_below_minimum", "recent_window_gap"]


def test_combine_decisions_four_component_weakest_wins():
    decisions = [
        cd(ClaimTier.SUPPORTED),
        cd(ClaimTier.SUPPORTED),
        cd(ClaimTier.SUGGESTIVE, "x"),
        cd(ClaimTier.INSUFFICIENT, "y"),
    ]
    assert combine_decisions(decisions).tier == ClaimTier.INSUFFICIENT


@pytest.mark.parametrize("n", [1, 2, 3, 5, 8])
def test_combine_decisions_accepts_n_components(n):
    tiers = [ClaimTier.SUPPORTED, ClaimTier.DETECTABLE_NOT_MEANINGFUL, ClaimTier.SUGGESTIVE, ClaimTier.INSUFFICIENT]
    decisions = [cd(tiers[i % len(tiers)], f"reason_{i}") for i in range(n)]
    out = combine_decisions(decisions)
    assert TIER_RANK[out.tier] == min(TIER_RANK[d.tier] for d in decisions)


def test_combine_decisions_is_permutation_invariant():
    baseline = cd(ClaimTier.SUPPORTED)
    trend = cd(ClaimTier.SUGGESTIVE, "recent_window_gap")
    correlation = cd(ClaimTier.INSUFFICIENT, "n_below_minimum")
    results = [combine_decisions(list(p)) for p in itertools.permutations([baseline, trend, correlation])]
    assert all(r == results[0] for r in results)


def test_combine_decisions_permutation_invariant_random():
    rng = random.Random(11)
    tiers = list(ClaimTier)
    for _ in range(200):
        decisions = [cd(rng.choice(tiers), *[f"r{i}"] * rng.randint(0, 2)) for i in range(rng.randint(1, 6))]
        shuffled = decisions[:]
        rng.shuffle(shuffled)
        assert combine_decisions(decisions) == combine_decisions(shuffled)


def test_combine_decisions_dedupes_shared_factors():
    a = cd(ClaimTier.SUGGESTIVE, "coverage_below_threshold")
    b = cd(ClaimTier.SUGGESTIVE, "coverage_below_threshold", "recent_window_gap")
    out = combine_decisions([a, b])
    assert out.limiting_factors == ["coverage_below_threshold", "recent_window_gap"]


# ---- authority: legacy confidence fields must be structurally unreachable ----
# Not a "does the code call this field" test — a signature test. If someone later adds a
# `confidence` or `sample_confidence` parameter to resolve() or combine_decisions() so a caller
# *could* pass one in, this fails immediately, before any value ever flows through it.
_FORBIDDEN_PARAM_NAMES = {"confidence", "sample_confidence"}


@pytest.mark.parametrize("fn", [resolve, combine_decisions], ids=lambda f: f.__name__)
def test_decision_functions_cannot_take_legacy_confidence_fields(fn):
    params = set(inspect.signature(fn).parameters)
    assert not (params & _FORBIDDEN_PARAM_NAMES), (
        f"{fn.__name__} gained a parameter named {params & _FORBIDDEN_PARAM_NAMES}; "
        "claim_decision must be derivable exclusively from assessed evidence dimensions "
        "(EvidenceProfile / ClaimDecision), never from a legacy confidence field."
    )
