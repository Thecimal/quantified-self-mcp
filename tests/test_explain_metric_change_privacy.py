"""
explain_metric_change must not surface a HEALTH_PRIVATE_FIELDS metric, directly or through anything derived from it.

The tool bundles several things for one metric and day: the value, a 90-day baseline, correlated metrics, evidence
and claim objects, narrative sentences, and (for workout_minutes) that day's logged workout sessions. A workout
session carries heart-rate fields (avg_heart_rate, max_heart_rate), which is the same quantity as the heart_rate
metric, so with heart_rate private they must not come back.

The same scenario runs twice. With nothing private, every permitted value must be visible (so the second run cannot
pass vacuously). With heart_rate private, none of its values, its source name or its name may appear anywhere in the
response, while the public metric and the session's other fields stay available.
"""

import json
import re
import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from eval.agent.runner import loaded_server  # noqa: E402

TARGET_DAY = "2026-05-08"
# Eight days of workout_minutes 30, 35, ..., 65 and a heart_rate that moves in lockstep (121, 124, ..., 142), so
# heart_rate would be the strongest correlate of workout_minutes (r = 1.0) if it were included.
MINUTES = [30 + 5 * k for k in range(8)]
HEART_RATE = [121 + 3 * k for k in range(8)]
SESSION_AVG_HR, SESSION_MAX_HR = 157, 193  # distinctive: occur in no other number the response contains
PRIVATE_SOURCE = "PrivateStrap"


def _hits(number: int, text: str) -> bool:
    return re.search(rf"(?<![\d.]){number}(?![\d])", text) is not None


async def _explain(tmp_path: Path, private: str) -> dict:
    tmp_path.mkdir()
    with loaded_server(tmp_path / "health.db", private_fields=private) as server:
        async with Client(server.mcp) as client:
            for k in range(8):
                await client.call_tool(
                    "log_daily_metric",
                    {"date": f"2026-05-{k + 1:02d}", "workout_minutes": MINUTES[k], "heart_rate": HEART_RATE[k]},
                )
            await client.call_tool(
                "log_measurement",
                {"timestamp": f"{TARGET_DAY}T09:00:00", "metric": "heart_rate", "value": 133, "source": PRIVATE_SOURCE},
            )
            await client.call_tool(
                "log_workout_session",
                {
                    "date": TARGET_DAY,
                    "activity_type": "run",
                    "duration_minutes": MINUTES[-1],
                    "avg_heart_rate": SESSION_AVG_HR,
                    "max_heart_rate": SESSION_MAX_HR,
                },
            )
            result = await client.call_tool("explain_metric_change", {"metric": "workout_minutes", "date": TARGET_DAY})
            if private:
                with pytest.raises(ToolError) as refused:
                    await client.call_tool("explain_metric_change", {"metric": private, "date": TARGET_DAY})
                assert not any(_hits(v, str(refused.value)) for v in HEART_RATE + [SESSION_AVG_HR, SESSION_MAX_HR])
    return result.structured_content


async def test_explain_metric_change_hides_private_heart_rate_and_keeps_permitted_data(tmp_path):
    open_view = await _explain(tmp_path / "open", private="")
    private_view = await _explain(tmp_path / "private", private="heart_rate")

    # --- the permitted data is identical either way: value, baseline, and the session's own fields ---
    for view in (open_view, private_view):
        assert view["metric"] == "workout_minutes" and view["date"] == TARGET_DAY
        assert view["value"] == 65.0
        # baseline of 30, 35, ..., 65: mean 47.5, median 47.5, population stdev sqrt(25 * (8**2 - 1) / 12) = 11.46
        assert view["baseline"] == {"n": 8, "mean": 47.5, "median": 47.5, "stdev": 11.46}
        assert [(s["activity_type"], s["duration_minutes"]) for s in view["sessions"]] == [("run", 65)]
        assert "Logged workout: run, 65 min" in " ".join(view["narrative_facts"])

    # --- control: with nothing private, the heart-rate data is genuinely reported ---
    open_text = json.dumps(open_view)
    assert [(s["avg_heart_rate"], s["max_heart_rate"]) for s in open_view["sessions"]] == [(157, 193)]
    assert "avg HR 157" in " ".join(open_view["narrative_facts"])
    assert [c["metric_b"] for c in open_view["correlated_metrics"]] == ["heart_rate"]
    assert open_view["correlated_metrics"][0]["r"] == 1.0
    assert _hits(SESSION_MAX_HR, open_text)

    # --- private heart_rate: not in the session rows, the narrative, the correlates, evidence or any metadata ---
    assert [(s["avg_heart_rate"], s["max_heart_rate"]) for s in private_view["sessions"]] == [(None, None)]
    assert private_view["correlated_metrics"] == []
    assert "HR" not in " ".join(private_view["narrative_facts"])
    text = json.dumps(private_view)
    leaked = [n for n in HEART_RATE + [SESSION_AVG_HR, SESSION_MAX_HR, 133] if _hits(n, text)]
    assert leaked == [], f"private heart_rate values surfaced: {leaked}"
    assert '"heart_rate"' not in text, "the private metric's name surfaced"  # exact name; avg_heart_rate keys are null
    assert PRIVATE_SOURCE not in text, "the private metric's source surfaced"
