from datetime import date, timedelta

import pytest

from qs_evidence import Dimension as D
from qs_evidence import Status as S
from qs_evidence import evaluate_sample, load_registry, sample

REG = load_registry()
START = date(2026, 1, 5)


def days(n):
    return [START + timedelta(days=i) for i in range(n)]


def wave(n, base=50.0):
    return [base + (i % 5) for i in range(n)]


# ---- window_comparison ------------------------------------------------------------------------


def test_window_adequate():
    r = sample.evaluate_window_comparison(wave(10), wave(10))
    assert r.dimension == D.SAMPLE and r.status == S.ADEQUATE and r.reason_codes == []


def test_window_small_n_blocks_and_none_nan_not_counted():
    r = sample.evaluate_window_comparison(wave(10), [1.0, None, float("nan")] * 3)  # only 3 usable
    assert r.status == S.BLOCKING and r.can_block and r.reason_codes == ["n_below_minimum"]
    failed = [c["name"] for c in r.details["checks"] if not c["passed"]]
    assert failed == ["n_window_b"]


def test_window_baseline_drift_is_diagnostic_only():
    r = sample.evaluate_window_comparison(wave(10), wave(10), baseline=[1.0] * 8 + [10.0] * 8)
    assert r.status == S.WEAK and not r.can_block and r.reason_codes == ["baseline_unstable"]
    r.model_dump_json()  # infinite drift must not leak into details


def test_window_stable_or_missing_baseline():
    assert sample.evaluate_window_comparison(wave(10), wave(10), baseline=wave(16)).status == S.ADEQUATE
    r = sample.evaluate_window_comparison(wave(10), wave(10))
    assert r.status == S.ADEQUATE and r.details["not_assessed"] == ["baseline_stability"]
    short = sample.evaluate_window_comparison(wave(10), wave(10), baseline=[1.0, 2.0, 3.0])
    assert short.details["not_assessed"] == ["baseline_stability"]


# ---- trend ------------------------------------------------------------------------------------


def test_trend_adequate():
    assert sample.evaluate_trend(days(30), wave(30)).status == S.ADEQUATE


def test_trend_span_blocks_even_when_n_is_fine():
    r = sample.evaluate_trend(days(15), wave(15))  # n=15 >= 14, span=14 < 21
    assert r.status == S.BLOCKING and r.reason_codes == ["span_below_minimum"]


def test_trend_small_n_blocks():
    r = sample.evaluate_trend(days(10), wave(10))
    assert r.status == S.BLOCKING and set(r.reason_codes) == {"n_below_minimum", "span_below_minimum"}


def test_trend_missing_value_drops_its_date():
    values = wave(30)
    values[18:] = [None] * 12  # 18 kept, but they only span 17 days
    r = sample.evaluate_trend(days(30), values)
    assert r.reason_codes == ["span_below_minimum"]
    span = [c for c in r.details["checks"] if c["name"] == "span_days"][0]
    assert span["observed"] == 17


def test_trend_length_mismatch_raises():
    with pytest.raises(ValueError):
        sample.evaluate_trend(days(5), wave(4))


# ---- correlation ------------------------------------------------------------------------------


def test_correlation_adequate():
    assert sample.evaluate_correlation(wave(25), wave(25), n_eff=20.0).status == S.ADEQUATE


def test_correlation_counts_only_complete_pairs():
    x = [1.0, None] * 20
    y = [None, 2.0] * 20  # 40 values each, zero complete pairs
    r = sample.evaluate_correlation(x, y)
    assert r.status == S.BLOCKING and r.reason_codes == ["n_paired_below_minimum"]
    assert r.details["checks"][0]["observed"] == 0


def test_n_eff_alone_can_never_block():
    r = sample.evaluate_correlation(wave(25), wave(25), n_eff=1.0)
    assert r.status == S.WEAK and r.can_block is False and r.reason_codes == ["n_eff_low"]


@pytest.mark.parametrize("n_eff", [0.0, 0.5, 2.0, 9.99, float("nan"), float("inf"), -3.0])
def test_n_eff_never_produces_blocking_for_any_value(n_eff):
    r = sample.evaluate_correlation(wave(25), wave(25), n_eff=n_eff)
    assert r.status != S.BLOCKING
    r.model_dump_json()


def test_low_n_paired_blocks_regardless_of_n_eff_and_keeps_both_reasons():
    r = sample.evaluate_correlation(wave(5), wave(5), n_eff=1.0)
    assert r.status == S.BLOCKING and r.can_block
    assert r.reason_codes == ["n_paired_below_minimum", "n_eff_low"]
    ok_eff = sample.evaluate_correlation(wave(5), wave(5), n_eff=500.0)
    assert ok_eff.status == S.BLOCKING and ok_eff.reason_codes == ["n_paired_below_minimum"]


def test_correlation_length_mismatch_raises():
    with pytest.raises(ValueError):
        sample.evaluate_correlation(wave(5), wave(6))


# ---- anomaly ----------------------------------------------------------------------------------


def test_anomaly_adequate():
    assert sample.evaluate_anomaly(wave(30)).status == S.ADEQUATE


def test_anomaly_short_baseline_blocks():
    r = sample.evaluate_anomaly(wave(20))
    assert r.status == S.BLOCKING and r.reason_codes == ["baseline_too_short"]


def test_anomaly_sparse_baseline_blocks():
    b = [v if i % 4 == 0 else None for i, v in enumerate(wave(40))]  # 40 days, 10 observations
    assert sample.evaluate_anomaly(b).reason_codes == ["baseline_too_few_observations"]


def test_anomaly_baseline_length_is_observed_span_not_window_length():
    long_window_short_history = wave(20) + [None] * 70  # 90-day window, 20 days of history
    r = sample.evaluate_anomaly(long_window_short_history)
    assert r.status == S.BLOCKING and r.reason_codes == ["baseline_too_short"]
    assert r.details["checks"][0]["observed"] == 20
    leading_gap = [None] * 60 + wave(30)  # a long empty lead-in does not count against a real 30-day history
    assert sample.evaluate_anomaly(leading_gap).status == S.ADEQUATE


def test_anomaly_zero_variance_blocks():
    r = sample.evaluate_anomaly([50.0] * 30)
    assert r.status == S.BLOCKING and r.reason_codes == ["baseline_zero_variance"]


def test_anomaly_contamination_is_diagnostic_only():
    b = wave(30)
    for i in (3, 9, 15, 21, 27):
        b[i] = 500.0
    r = sample.evaluate_anomaly(b)
    assert r.status == S.WEAK and not r.can_block and r.reason_codes == ["baseline_contaminated"]


def test_anomaly_mad_zero_contamination_not_assessed():
    b = [50.0] * 28 + [55.0, 45.0]
    r = sample.evaluate_anomaly(b)
    assert r.status == S.ADEQUATE and "baseline_contamination" in r.details["not_assessed"]


# ---- baseline ---------------------------------------------------------------------------------------


def test_baseline_length_and_observation_count_block():
    assert sample.evaluate_baseline(wave(30)).status == S.ADEQUATE
    short = sample.evaluate_baseline(wave(20))
    assert short.status == S.BLOCKING and "baseline_too_short" in short.reason_codes
    sparse = sample.evaluate_baseline([50.0, 51.0] + [None] * 26 + [52.0, 53.0])  # long span, only 4 observations
    assert sparse.status == S.BLOCKING and sparse.reason_codes == ["baseline_too_few_observations"]


def test_baseline_zero_variance_is_adequate_unlike_anomaly():
    flat = [50.0] * 30
    assert sample.evaluate_baseline(flat).status == S.ADEQUATE
    assert sample.evaluate_anomaly(flat).status == S.BLOCKING


def test_baseline_drift_and_contamination_are_diagnostic_only():
    drifting = sample.evaluate_baseline(wave(15) + wave(15, base=80.0))
    assert drifting.status == S.WEAK and drifting.can_block is False
    assert drifting.reason_codes == ["baseline_unstable"]
    contaminated = sample.evaluate_baseline(wave(24) + [500.0, -400.0, 600.0, -300.0, 700.0, -500.0])
    assert contaminated.can_block is False and "baseline_contaminated" in contaminated.reason_codes


# ---- dispatch / registry ----------------------------------------------------------------------


def test_dispatch_uses_registry_thresholds():
    spec = REG["trend"]
    assert evaluate_sample("trend", spec.thresholds, dates=days(30), values=wave(30)).status == S.ADEQUATE
    strict = {**spec.thresholds, "min_n": 40}
    assert evaluate_sample("trend", strict, dates=days(30), values=wave(30)).status == S.BLOCKING


def test_dispatch_covers_every_registry_analysis_that_lists_sample():
    listed = {n for n, s in REG.items() if D.SAMPLE in s.dimensions}
    for name in listed:
        with pytest.raises(TypeError):  # known analysis, but no inputs supplied
            evaluate_sample(name, REG[name].thresholds)
    with pytest.raises(ValueError):
        evaluate_sample("not_an_analysis")
