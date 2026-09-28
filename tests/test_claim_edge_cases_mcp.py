"""Edge-case claims verified on the wire: the numeric result and its claim, through a real MCP client.

Internal assessment tests (qs_evidence/tests/test_edge_cases.py) prove the evaluator is right; these prove the
final tool response a model actually reads carries both the number and a claim that does not outrun it.
"""

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastmcp import Client

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qs_evidence.models import TIER_RANK, ClaimTier  # noqa: E402

START = date(2026, 3, 1)


def _rank(decision: dict) -> int:
    return TIER_RANK[ClaimTier(decision["tier"])]


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


def _seed(server, metric: str, values: list, start: date = START) -> None:
    for i, v in enumerate(values):
        if v is not None:
            server.log_daily_metric(date=(start + timedelta(days=i)).isoformat(), **{metric: v})


def _window(n: int, start: date = START) -> dict:
    return {"start_date": start.isoformat(), "end_date": (start + timedelta(days=n - 1)).isoformat()}


def _noisy(n: int, base: int = 5000) -> list[int]:
    return [base + ((i * 7) % 11) * 100 for i in range(n)]


def _assert_claim_matches_legacy_mirror(sc: dict) -> None:
    assert sc["claim"]["decision"] == sc["claim_decision"]


# ---- anomaly ------------------------------------------------------------------------------------


@pytest.mark.parametrize("n", [40, 60])
async def test_flat_series_with_one_extreme_is_reported_as_unable_to_detect(client, health_db, n):
    values = [5000] * n
    values[n // 2] = 95000
    _seed(health_db, "steps", values)
    sc = (await client.call_tool("detect_metric_anomalies", {"metric": "steps", **_window(n)})).structured_content

    # numeric result: the detector really returns nothing ...
    assert sc["anomalies"] == []
    # ... and the claim must not let that read as "nothing unusual happened".
    decision = sc["claim"]["decision"]
    assert decision["tier"] == "insufficient"
    assert "baseline_mad_zero" in decision["must_state"]
    sample = next(d for d in sc["claim"]["profile"]["dimensions"] if d["dimension"] == "sample")
    assert sample["status"] == "blocking"
    _assert_claim_matches_legacy_mirror(sc)


async def test_genuinely_uneventful_anomaly_window_is_not_marked_unable(client, health_db):
    _seed(health_db, "steps", _noisy(40))
    sc = (await client.call_tool("detect_metric_anomalies", {"metric": "steps", **_window(40)})).structured_content
    assert sc["anomalies"] == []
    assert "baseline_mad_zero" not in sc["claim"]["decision"]["must_state"]
    assert "baseline_zero_variance" not in sc["claim"]["decision"]["must_state"]


async def test_detected_anomaly_keeps_its_number_and_a_claim(client, health_db):
    values = _noisy(40)
    values[30] = 40000
    _seed(health_db, "steps", values)
    sc = (await client.call_tool("detect_metric_anomalies", {"metric": "steps", **_window(40)})).structured_content
    assert [a["value"] for a in sc["anomalies"]] == [40000]
    assert sc["claim"]["decision"]["tier"] != "insufficient"
    assert "baseline_mad_zero" not in sc["claim"]["decision"]["must_state"]


# ---- trend --------------------------------------------------------------------------------------


async def test_sparse_series_with_steep_slope_reports_its_limits(client, health_db):
    _seed(health_db, "steps", [1000 * (i + 1) for i in range(8)])
    sc = (await client.call_tool("calculate_metric_trend", {"metric": "steps", **_window(8)})).structured_content
    assert sc["trend"]["direction"] == "increasing" and sc["trend"]["slope_per_day"] == 1000.0
    decision = sc["claim"]["decision"]
    assert decision["tier"] == "insufficient"
    assert "n_below_minimum" in decision["must_state"]
    _assert_claim_matches_legacy_mirror(sc)


async def test_trend_over_short_span_is_insufficient_even_with_dense_data(client, health_db):
    # Dense, but only 10 days of history: the count and the calendar span are separate requirements.
    _seed(health_db, "steps", _noisy(10))
    sc = (await client.call_tool("calculate_metric_trend", {"metric": "steps", **_window(10)})).structured_content
    assert sc["claim"]["decision"]["tier"] == "insufficient"


async def test_full_trend_is_never_supported_while_robustness_is_unevaluated(client, health_db):
    values = _noisy(40)
    values[-1] = 90000  # an extreme final point can drive the OLS slope
    _seed(health_db, "steps", values)
    sc = (await client.call_tool("calculate_metric_trend", {"metric": "steps", **_window(40)})).structured_content
    decision = sc["claim"]["decision"]
    assert decision["tier"] != "supported"
    assert "robustness_not_assessed" in decision["must_state"]


# ---- correlation --------------------------------------------------------------------------------


async def test_high_correlation_with_very_few_pairs_is_not_overstated(client, health_db):
    _seed(health_db, "steps", [1000, 2000, 3000, 4000, 5000])
    _seed(health_db, "water_ml", [1500, 2000, 2500, 3000, 3500])
    sc = (
        await client.call_tool(
            "find_metric_correlation", {"metric_a": "steps", "metric_b": "water_ml", **_window(5)}
        )
    ).structured_content
    assert sc["r"] == 1.0 and sc["n"] == 5
    assert sc["claim"]["decision"]["tier"] == "insufficient"
    assert "n_paired_below_minimum" in sc["claim"]["decision"]["must_state"]


async def test_correlation_with_a_constant_series_reports_no_relationship_estimable(client, health_db):
    _seed(health_db, "steps", _noisy(30))
    _seed(health_db, "water_ml", [2000] * 30)
    sc = (
        await client.call_tool(
            "find_metric_correlation", {"metric_a": "steps", "metric_b": "water_ml", **_window(30)}
        )
    ).structured_content
    assert sc["r"] is None
    assert sc["claim"]["decision"]["tier"] == "insufficient"
    assert "zero_variance" in sc["claim"]["decision"]["must_state"]


# ---- composite ----------------------------------------------------------------------------------


async def test_explain_metric_change_inherits_the_weakest_included_component(client, health_db):
    target = START + timedelta(days=89)
    # 60 dense days of history (anomaly baseline is fine), then only 3 readings in the trend window.
    values: list = _noisy(60) + [None] * 27 + [5000, None, 5100]
    _seed(health_db, "steps", values)
    sc = (
        await client.call_tool("explain_metric_change", {"metric": "steps", "date": target.isoformat()})
    ).structured_content

    trend = sc["trend_claim"]["decision"]
    headline = sc["headline_claim"]["decision"]
    overall = sc["overall_decision"]
    assert trend["tier"] == "insufficient"
    assert _rank(overall) <= min(_rank(trend), _rank(headline))
    assert overall["tier"] == "insufficient"
    # every included component's limits are carried into what must be stated
    assert set(trend["must_state"]) | set(headline["must_state"]) <= set(overall["must_state"])


async def test_explain_metric_change_overall_carries_the_baseline_coverage_limits(client, health_db):
    """The response reports baseline statistics (mean/median/stdev over 90 days), so the composite must carry
    that baseline's own limits, not only the anomaly and trend claims'."""
    target = START + timedelta(days=89)
    # Sparse early baseline (one reading every 4 days), dense final 31 days. The trend window is therefore
    # well covered on its own, so a coverage caveat can only come from the 90-day baseline.
    values: list = [5000 + (i % 5) * 100 if i % 4 == 0 else None for i in range(59)]
    values += [5000 + ((i * 7) % 11) * 100 for i in range(31)]
    _seed(health_db, "steps", values)
    base = (
        await client.call_tool(
            "get_baseline", {"metric": "steps", "start_date": START.isoformat(), "end_date": target.isoformat()}
        )
    ).structured_content
    assert "coverage_below_threshold" in base["claim"]["decision"]["must_state"]

    sc = (
        await client.call_tool("explain_metric_change", {"metric": "steps", "date": target.isoformat()})
    ).structured_content
    assert set(base["claim"]["decision"]["must_state"]) <= set(sc["overall_decision"]["must_state"])
    assert _rank(sc["overall_decision"]) <= _rank(base["claim"]["decision"])
