import random

import pytest

from qs_evidence import ClaimTier, DimensionResult, EvidenceProfile, load_registry, resolve
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
