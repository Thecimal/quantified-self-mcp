"""
read_health_data end to end: logged observations -> daily aggregation -> MCP response.

One synthetic 10-day window (2026-04-01 .. 2026-04-10), written only through the public logging tools and read back
through a real MCP client. Expected values are worked out by hand below, not read back from the code.

Window shape (d = day of April):
  - d6  : nothing logged for any metric (a true gap).
  - d10 : only weight_kg, which is private (HEALTH_PRIVATE_FIELDS), so it is no day with data at all.
  - d2  : weight_kg also logged next to public metrics; its value must never appear.

Per-metric days logged (each is a percentage of the 10 expected days):
  steps 8 (80), sleep_hours 8 (80), resting_heart_rate 6 (60), workout_minutes 6 (60), mood 6 (60),
  water_ml 6 (60), heart_rate 4 (40), hrv_ms 4 (40).

The 8 public metrics average (80+80+60+60+60+60+40+40) / 8 = 60.0, exactly the "moderate" threshold. Counting the
private weight_kg (20 percent) as a ninth metric would give 500 / 9 = 55.6 and flip the verdict to "low", so the
confidence assertion also pins down that private metrics stay out of the evidence basis.
"""

import json
import sys
from pathlib import Path

from fastmcp import Client

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from eval.agent.runner import loaded_server  # noqa: E402
from schemas import ReadHealthDataResult  # noqa: E402

WINDOW = {"start_date": "2026-04-01", "end_date": "2026-04-10"}
PRIVATE_WEIGHT = 71.3571  # distinctive: must not occur anywhere in the response

# Raw readings, several per day: steps are summed, resting heart rate is averaged.
STEP_DAYS = (1, 2, 3, 4, 5, 7, 8, 9)  # readings 1000*d and 500 -> day total 1000*d + 500
RHR_DAYS = (1, 2, 3, 4, 7, 8)  # readings 60 and 60 + 2*d -> day mean 60 + d (a sum would be 120 + 2*d)

# One value per day through log_daily_metric.
DAILY: dict[str, dict[int, float]] = {
    "sleep_hours": {1: 6.5, 2: 7.0, 3: 7.5, 4: 8.0, 5: 8.5, 7: 9.5, 8: 10.0, 9: 10.5},
    "workout_minutes": {1: 40, 2: 50, 3: 60, 4: 70, 5: 80, 7: 100},
    "mood": {1: 4, 2: 6, 3: 5, 7: 8, 8: 9, 9: 7},
    "water_ml": {1: 1500, 2: 2000, 3: 2500, 7: 1800, 8: 2200, 9: 2400},
    "heart_rate": {1: 72, 2: 75, 3: 78, 4: 81},
    "hrv_ms": {1: 40.5, 2: 42.0, 3: 45.5, 4: 48.0},
}

# Hand-worked. steps: (1500+2500+3500+4500+5500+7500+8500+9500) / 8 = 43000 / 8. resting_heart_rate:
# (61+62+63+64+67+68) / 6 = 385 / 6 = 64.17. sleep_hours: 67.5 / 8 = 8.4375. workout_minutes: 400 / 6 = 66.67.
# mood: 39 / 6. water_ml: 12400 / 6 = 2066.67. heart_rate: 306 / 4. hrv_ms: 176 / 4.
EXPECTED_SUMMARY = {
    "steps": {"avg": 5375.0, "min": 1500, "max": 9500},
    "resting_heart_rate": {"avg": 64.2, "min": 61, "max": 68},
    "sleep_hours": {"avg": 8.4, "min": 6.5, "max": 10.5},
    "workout_minutes": {"avg": 66.7, "min": 40, "max": 100},
    "mood": {"avg": 6.5, "min": 4, "max": 9},
    "water_ml": {"avg": 2066.7, "min": 1500, "max": 2500},
    "heart_rate": {"avg": 76.5, "min": 72, "max": 81},
    "hrv_ms": {"avg": 44.0, "min": 40.5, "max": 48.0},
    "weight_kg": {"avg": None, "min": None, "max": None},  # private
}
EXPECTED_COVERAGE_PERCENT = {
    "steps": 80.0,
    "sleep_hours": 80.0,
    "resting_heart_rate": 60.0,
    "workout_minutes": 60.0,
    "mood": 60.0,
    "water_ml": 60.0,
    "heart_rate": 40.0,
    "hrv_ms": 40.0,
}
EXPECTED_DAYS = [
    "2026-04-01",
    "2026-04-02",
    "2026-04-03",
    "2026-04-04",
    "2026-04-05",
    "2026-04-07",
    "2026-04-08",
    "2026-04-09",
]


def _day(d: int) -> str:
    return f"2026-04-{d:02d}"


async def _log_everything(client: Client) -> None:
    for d in STEP_DAYS:
        for hour, value in (("08", 1000 * d), ("18", 500)):
            await client.call_tool(
                "log_measurement",
                {"timestamp": f"{_day(d)}T{hour}:00:00", "metric": "steps", "value": value, "source": "Watch"},
            )
    for d in RHR_DAYS:
        for hour, value in (("07", 60), ("19", 60 + 2 * d)):
            await client.call_tool(
                "log_measurement",
                {
                    "timestamp": f"{_day(d)}T{hour}:00:00",
                    "metric": "resting_heart_rate",
                    "value": value,
                    "source": "Watch",
                },
            )
    for d in range(1, 10):
        fields = {metric: by_day[d] for metric, by_day in DAILY.items() if d in by_day}
        if d == 2:
            fields["weight_kg"] = PRIVATE_WEIGHT
        if fields:
            await client.call_tool("log_daily_metric", {"date": _day(d), **fields})
    await client.call_tool("log_daily_metric", {"date": _day(10), "weight_kg": PRIVATE_WEIGHT})


async def test_read_health_data_values_gaps_evidence_and_schema(tmp_path):
    with loaded_server(tmp_path / "health.db", private_fields="weight_kg") as server:
        async with Client(server.mcp) as client:
            await _log_everything(client)
            result = await client.call_tool("read_health_data", WINDOW)
            tool = next(t for t in await client.list_tools() if t.name == "read_health_data")
    payload = result.structured_content

    # --- response contract: top-level keys match the advertised output schema and re-validate as the model ---
    assert (
        set(payload) == set(tool.output_schema["properties"]) == {"range", "rows", "truncated", "summary", "coverage"}
    )
    assert ReadHealthDataResult.model_validate(payload).model_dump() == payload
    assert payload["range"] == WINDOW
    assert payload["truncated"] is False

    # --- rows: only days with a public observation; the gap (d6) and the private-only day (d10) are absent ---
    rows = {r["date"]: r for r in payload["rows"]}
    assert list(rows) == EXPECTED_DAYS
    assert rows["2026-04-01"] == {
        "date": "2026-04-01",
        "steps": 1500,  # 1000 + 500, summed
        "sleep_hours": 6.5,
        "resting_heart_rate": 61,  # mean(60, 62), not the sum
        "weight_kg": None,
        "workout_minutes": 40,
        "mood": 4,
        "water_ml": 1500,
        "heart_rate": 72,
        "hrv_ms": 40.5,
    }
    assert rows["2026-04-02"]["weight_kg"] is None  # stored, but redacted next to public data
    assert rows["2026-04-09"]["steps"] == 9500
    assert rows["2026-04-09"]["resting_heart_rate"] is None  # a missing metric is null, never 0
    assert rows["2026-04-05"]["heart_rate"] is None

    # --- summary: averages over logged days only, never diluted by missing days ---
    summary = payload["summary"]
    assert summary["days_with_data"] == 8
    assert {k: v for k, v in summary.items() if k != "days_with_data"} == EXPECTED_SUMMARY

    # --- evidence: coverage counts only days with public data, and the verdict follows the data ---
    coverage = payload["coverage"]
    assert coverage["period"] == "2026-04-01/2026-04-10"
    assert (coverage["days_expected"], coverage["days_with_data"], coverage["missing_days"]) == (10, 8, 2)
    assert coverage["coverage_percent"] == 80.0
    assert coverage["metrics"] == EXPECTED_COVERAGE_PERCENT  # private weight_kg is not listed at all
    assert coverage["confidence"] == "moderate"  # mean of the 8 public metrics is exactly 60.0

    # --- privacy: the private value appears nowhere in the serialized response ---
    text = json.dumps(payload)
    assert "71.3571" not in text and "71.4" not in text
