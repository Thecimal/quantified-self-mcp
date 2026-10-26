"""
Freshness in data_health: which metrics are "stale", and after how long.

The repo has one freshness policy (source_coverage.py): daily metrics are stale after DEFAULT_STALE_AFTER_DAYS (2),
episodic metrics (weight, workouts) are not expected daily and are stale after EPISODIC_STALE_AFTER_DAYS (30).
data_health used to apply the 2-day limit to every metric, so a weigh-in from last week made get_metric_history,
get_baseline and the other analytics report STALE while get_data_status and the coverage matrix called the same data
current. Freshness stays informational (it never changes a claim tier); only the stale threshold is under test.

The reference time is fixed: every window ends on END, and freshness is measured from the window end, so nothing
here depends on today's date.
"""

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastmcp import Client

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from eval.agent.runner import loaded_server  # noqa: E402
from logic import DEFAULT_STALE_AFTER_DAYS  # noqa: E402
from source_coverage import EPISODIC_METRICS, EPISODIC_STALE_AFTER_DAYS  # noqa: E402

END = date(2026, 3, 15)
WINDOW = {"start_date": (END - timedelta(days=89)).isoformat(), "end_date": END.isoformat()}
STALE_REASON = "latest_observation_older_than_threshold"
VALUE = {"steps": 7000, "weight_kg": 80.0, "workout_minutes": 30}


async def _history_health(tmp_path, metric, newest_age, cadence_days):
    """data_health of get_metric_history over a 90-day window whose newest reading is newest_age days old and
    which has one reading every cadence_days before that (so the window also contains missing days)."""
    ages = range(newest_age, 90, cadence_days)
    with loaded_server(tmp_path / f"{metric}-{newest_age}.db") as server:
        async with Client(server.mcp) as client:
            for age in ages:
                day = (END - timedelta(days=age)).isoformat()
                await client.call_tool("log_daily_metric", {"date": day, metric: VALUE[metric]})
            result = await client.call_tool("get_metric_history", {"metric": metric, **WINDOW})
    return result.structured_content["data_health"]


def test_the_two_limits_are_the_documented_policy():
    assert (DEFAULT_STALE_AFTER_DAYS, EPISODIC_STALE_AFTER_DAYS) == (2, 30)
    assert EPISODIC_METRICS == {"weight_kg", "workout_minutes"}


@pytest.mark.parametrize("metric", sorted(EPISODIC_METRICS))
async def test_an_episodic_metric_is_current_until_its_own_limit(tmp_path, metric):
    # recent data, weekly cadence: a weigh-in or workout 5 days ago is current, not stale
    recent = await _history_health(tmp_path, metric, newest_age=5, cadence_days=7)
    assert recent["freshness"]["age_days"] == 5  # the age is still reported as observed
    assert recent["status"] == "VALID_WITH_GAPS"
    assert STALE_REASON not in recent["reasons"]
    assert "gaps_in_window" in recent["reasons"]  # the missing days are still reported

    # exactly at the limit is current; one day past it is stale (the limit is exclusive, like the daily one)
    at_limit = await _history_health(tmp_path, metric, newest_age=EPISODIC_STALE_AFTER_DAYS, cadence_days=7)
    assert at_limit["status"] == "VALID_WITH_GAPS" and STALE_REASON not in at_limit["reasons"]
    past_limit = await _history_health(tmp_path, metric, newest_age=EPISODIC_STALE_AFTER_DAYS + 1, cadence_days=7)
    assert past_limit["status"] == "STALE"
    assert STALE_REASON in past_limit["reasons"]


async def test_a_daily_metric_keeps_the_two_day_limit(tmp_path):
    at_limit = await _history_health(tmp_path, "steps", newest_age=DEFAULT_STALE_AFTER_DAYS, cadence_days=1)
    assert at_limit["status"] == "VALID_WITH_GAPS" and STALE_REASON not in at_limit["reasons"]
    stale = await _history_health(tmp_path, "steps", newest_age=DEFAULT_STALE_AFTER_DAYS + 1, cadence_days=1)
    assert stale["status"] == "STALE" and STALE_REASON in stale["reasons"]
    assert (await _history_health(tmp_path, "steps", newest_age=5, cadence_days=1))["status"] == "STALE"


async def test_get_baseline_reports_the_same_staleness_for_a_weekly_metric(tmp_path):
    # the claim path: weekly weight is too sparse for a baseline (insufficient), but its data_health must not
    # also claim the data is stale; it is 5 days old on a metric that is current for 30
    with loaded_server(tmp_path / "baseline.db") as server:
        async with Client(server.mcp) as client:
            for age in range(5, 90, 7):
                day = (END - timedelta(days=age)).isoformat()
                await client.call_tool("log_daily_metric", {"date": day, "weight_kg": 80.0})
            sc = (await client.call_tool("get_baseline", {"metric": "weight_kg", **WINDOW})).structured_content
    assert sc["claim"]["decision"]["tier"] == "insufficient"
    assert sc["data_health"]["status"] == "INSUFFICIENT_DATA"
    assert STALE_REASON not in sc["data_health"]["reasons"]
