import random
from datetime import date

from qs_evidence import (
    ClaimTier,
    DimensionResult,
    EvidenceProfile,
    evaluate_missingness,
    evaluate_temporal,
    load_registry,
    resolve,
)
from qs_evidence import Dimension as D
from qs_evidence import Status as S

START = date(2026, 1, 5)  # Monday
N = 60
SPEC = load_registry()["window_comparison"]


def series(missing_idx=()):
    m = set(missing_idx)
    return [None if i in m else 50.0 + (i % 7) for i in range(N)]


def test_clean_series_adequate():
    assert evaluate_temporal(series(), START).status == S.ADEQUATE
    assert evaluate_missingness(series()).status == S.ADEQUATE


def test_71pct_overall_hides_recent_collapse():
    # thirds coverage ~ 100% / 100% / 13%  (overall ~72%)
    v = series(range(43, 60))
    t, m = evaluate_temporal(v, START), evaluate_missingness(v)
    assert t.status == S.WEAK
    assert "recent_window_gap" in t.reason_codes
    assert "uneven_coverage_across_window" in t.reason_codes
    assert t.details["third_coverage"][:2] == [1.0, 1.0] and t.details["third_coverage"][2] < 0.2
    assert m.details["coverage"] > 0.7 and "coverage_below_threshold" not in m.reason_codes


def test_old_gap_is_long_gap_not_recent():
    t = evaluate_temporal(series(range(5, 12)), START)
    assert "long_gap" in t.reason_codes and "recent_window_gap" not in t.reason_codes


def test_weekday_weekend_imbalance():
    v = [None if (START.toordinal() + i - date(2026, 1, 5).toordinal()) % 7 >= 5 else 55.0 for i in range(N)]
    assert "weekday_weekend_imbalance" in evaluate_temporal(v, START).reason_codes


def test_random_missingness_not_clustered():
    rng = random.Random(3)
    v = series(rng.sample(range(N), 12))
    assert "clustered_missing" not in evaluate_missingness(v).reason_codes


def test_clustered_missing_is_not_mnar():
    v = series(range(20, 34))  # one contiguous block, 77% coverage
    m = evaluate_missingness(v, covariate=[7.0] * N, covariate_name="sleep")
    assert "clustered_missing" in m.reason_codes
    assert m.details["mnar"]["state"] != "association_detected"


def test_mnar_association_detected():
    rng = random.Random(1)
    miss = set(rng.sample(range(N), 14))
    sleep = [5.0 + rng.random() if i in miss else 7.5 + rng.random() for i in range(N)]
    m = evaluate_missingness(series(miss), covariate=sleep, covariate_name="low_sleep")
    assert m.details["mnar"]["state"] == "association_detected"
    assert m.status == S.CONCERN and "missing_correlates_with_low_sleep" in m.reason_codes


def test_mnar_no_association_and_not_assessed_states():
    rng = random.Random(2)
    miss = set(rng.sample(range(N), 14))
    sleep = [7.0 + rng.random() for _ in range(N)]
    assert evaluate_missingness(series(miss), sleep).details["mnar"]["state"] == "no_association_found"
    assert evaluate_missingness(series(miss)).details["mnar"]["state"] == "not_assessed"


def test_too_few_observations_not_assessed():
    v = [None] * (N - 2) + [50.0, 51.0]
    assert evaluate_temporal(v, START).status == S.NOT_ASSESSED


def _profile(v, **kw):
    dims = {d: DimensionResult(dimension=d, status=S.ADEQUATE) for d in D}
    dims[D.TEMPORAL] = evaluate_temporal(v, START)
    dims[D.MISSINGNESS] = evaluate_missingness(v, **kw)
    return EvidenceProfile(metric="hrv_rmssd", analysis="window_comparison", dimensions=list(dims.values()))


def test_same_effect_different_evidence_end_to_end():
    assert resolve(_profile(series()), SPEC).tier == ClaimTier.SUPPORTED
    d = resolve(_profile(series(range(43, 60))), SPEC)
    assert d.tier == ClaimTier.SUGGESTIVE and "recent_window_gap" in d.limiting_factors
