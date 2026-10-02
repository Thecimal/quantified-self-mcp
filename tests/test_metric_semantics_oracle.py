"""
Oracle test: analytics must compute from the registry's daily semantics.

Several raw readings per day are logged for every metric through the real MCP
tools. The expected daily value is derived here, independently, from
metric_registry.MetricDefinition.aggregation (sum / mean / last); the expected
period figures are then computed by hand and compared with what
get_baseline, compare_metric_periods, calculate_metric_trend and
find_metric_correlation return. A wrong rollup (e.g. mean where the registry
says sum) changes the number and fails here, however good its evidence is.
"""

import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastmcp import Client

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from metric_registry import METRICS  # noqa: E402

TODAY = date(2026, 9, 28)


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HEALTH_DB_PATH", str(tmp_path / "health.db"))
    monkeypatch.delenv("HEALTH_PRIVATE_FIELDS", raising=False)
    for mod in ("server", "privacy", "tools.health", "tools.measurements", "tools.workouts"):
        sys.modules.pop(mod, None)
    import server

    async with Client(server.mcp) as c:
        yield c


def _readings(metric: str, day_index: int) -> list[float]:
    """Three readings for the day, distinct enough that sum/mean/last all differ."""
    lo, hi = METRICS[metric].min_value, METRICS[metric].max_value
    base = lo + (hi - lo) * 0.05 * (1 + (day_index % 5))
    vals = [base, base * 1.5, base * 2.0] if metric != "mood" else [3.0, 5.0, 7.0]
    return [round(min(max(v, lo), hi), 1) for v in vals]


def _daily(metric: str, readings: list[float]) -> float:
    rule = METRICS[metric].aggregation
    return {"sum": sum(readings), "mean": statistics.fmean(readings), "last": readings[-1]}[rule]


async def _seed(client, metric: str, days: list[date]) -> dict[date, float]:
    expected = {}
    for i, day in enumerate(days):
        readings = _readings(metric, i)
        for hour, value in zip((8, 12, 18), readings, strict=True):
            await client.call_tool(
                "log_measurement",
                {"timestamp": f"{day.isoformat()}T{hour:02d}:00:00", "metric": metric, "value": value, "source": "dev"},
            )
        expected[day] = _daily(metric, readings)
    return expected


@pytest.mark.parametrize("metric", list(METRICS))
async def test_period_comparison_uses_the_registrys_daily_rollup(client, metric):
    days_a = [TODAY - timedelta(days=i) for i in range(6, -1, -1)]  # 7 days, current
    days_b = [TODAY - timedelta(days=30 + i) for i in range(13, -1, -1)]  # 14 days, baseline (unequal length)
    exp_a = await _seed(client, metric, days_a)
    exp_b = await _seed(client, metric, days_b)
    mean_a, mean_b = statistics.fmean(exp_a.values()), statistics.fmean(exp_b.values())

    result = (
        await client.call_tool(
            "compare_metric_periods",
            {
                "metric": metric,
                "period_a_start": days_a[0].isoformat(),
                "period_a_end": days_a[-1].isoformat(),
                "period_b_start": days_b[0].isoformat(),
                "period_b_end": days_b[-1].isoformat(),
            },
        )
    ).structured_content

    assert result["period_a_stats"]["mean"] == pytest.approx(mean_a, abs=0.01)
    assert result["period_b_stats"]["mean"] == pytest.approx(mean_b, abs=0.01)
    assert result["delta"] == pytest.approx(round(mean_a - mean_b, 2), abs=0.02)
    assert result["period_a_stats"]["n"] == 7
    assert result["period_b_stats"]["n"] == 14


@pytest.mark.parametrize("metric", list(METRICS))
async def test_baseline_and_trend_use_the_registrys_daily_rollup(client, metric):
    days = [TODAY - timedelta(days=i) for i in range(20, -1, -1)]
    expected = await _seed(client, metric, days)
    values = [expected[d] for d in days]
    window = {"start_date": days[0].isoformat(), "end_date": days[-1].isoformat()}

    base = (await client.call_tool("get_baseline", {"metric": metric, **window})).structured_content["baseline"]
    assert base["mean"] == pytest.approx(statistics.fmean(values), abs=0.01)
    assert base["median"] == pytest.approx(statistics.median(values), abs=0.01)

    trend = (await client.call_tool("calculate_metric_trend", {"metric": metric, **window})).structured_content["trend"]
    xs = list(range(len(values)))
    slope = statistics.linear_regression(xs, values).slope
    assert trend["slope_per_day"] == pytest.approx(slope, abs=0.02, rel=0.01)


@pytest.mark.parametrize(("metric_a", "metric_b"), [("steps", "weight_kg"), ("sleep_hours", "resting_heart_rate")])
async def test_correlation_joins_daily_canonical_values_of_mixed_rollups(client, metric_a, metric_b):
    """Different rollups (sum vs last, sum vs mean): both sides must be the registry's daily value."""
    days = [TODAY - timedelta(days=i) for i in range(24, -1, -1)]
    exp_a = await _seed(client, metric_a, days)
    exp_b = await _seed(client, metric_b, days)
    expected_r = round(statistics.correlation([exp_a[d] for d in days], [exp_b[d] for d in days]), 3)
    result = (
        await client.call_tool(
            "find_metric_correlation",
            {
                "metric_a": metric_a,
                "metric_b": metric_b,
                "start_date": days[0].isoformat(),
                "end_date": days[-1].isoformat(),
            },
        )
    ).structured_content
    assert result["n"] == len(days)
    assert result["r"] == pytest.approx(expected_r, abs=0.002)
