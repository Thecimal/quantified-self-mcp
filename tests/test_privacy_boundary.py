"""
Privacy-boundary audit: HEALTH_PRIVATE_FIELDS must hold on every output path.

1. Every registered tool is classified below. A new tool fails
   test_every_registered_tool_has_a_declared_privacy_boundary until someone
   states how it treats private metrics.
2. A sweep stores a distinctive sentinel under a private metric, calls every
   tool and resource through the real MCP client (including with the private
   metric as an argument), and checks tool results, error messages, resources,
   the exported CSV file, and server logs for the sentinel.
3. One strict xfail records a confirmed bypass (workout sessions); it turns red
   when the bypass is fixed so the marker must be removed deliberately.
"""

import json
import logging
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# How each tool treats a metric listed in HEALTH_PRIVATE_FIELDS.
REDACTS = "redacts the private metric's value (null) in its output"
REFUSES = "refuses a private metric outright (error), never computes from it"
EXCLUDES = "silently skips private metrics when it iterates over all metrics"
NO_PRIVATE_DATA = "returns no metric values (writes only, or reads a non-metric table)"
KNOWN_GAP = "KNOWN GAP: see the strict xfail below"

PRIVACY_BOUNDARY = {
    "read_health_data": REDACTS,
    "export_health_data_csv": REDACTS,
    "log_daily_metric": REDACTS,
    "clear_metric": REDACTS,
    "log_measurement": REDACTS,
    "read_measurements": REDACTS,
    "aggregate_measurements": EXCLUDES,
    "get_metric_provenance": REFUSES,
    "get_metric_history": REFUSES,
    "get_baseline": REFUSES,
    "detect_metric_anomalies": REFUSES,
    "calculate_metric_trend": REFUSES,
    "compare_metric_periods": REFUSES,
    "find_metric_correlation": REFUSES,
    "get_recent_changes": EXCLUDES,
    "explain_metric_change": EXCLUDES,
    "get_data_status": EXCLUDES,
    "get_import_status": NO_PRIVATE_DATA,
    "log_workout_session": KNOWN_GAP,
    "read_workout_sessions": KNOWN_GAP,
}

# Distinctive values: a float and a 4-digit int that do not occur in ids,
# dates or counts. Matched on number boundaries, and a row id ("id": 77) is not a hit.
SENTINELS = {"weight_kg": 77.3173, "water_ml": 9137}


def _hits(sentinel, text: str) -> list[str]:
    forms = (
        {str(sentinel), str(round(sentinel, 1)), str(round(sentinel))}
        if isinstance(sentinel, float)
        else {str(sentinel)}
    )
    found = []
    for form in forms:
        for m in re.finditer(r'(?<!"id": )(?<![\d.])' + re.escape(form) + r"(?![\d])", text):
            found.append(f"{form!r} near ...{text[max(0, m.start() - 50) : m.end() + 15]}")
    return found


@pytest.fixture
def make_server(tmp_path, monkeypatch):
    def _make(private: str):
        monkeypatch.setenv("HEALTH_DB_PATH", str(tmp_path / "health.db"))
        monkeypatch.setenv("HEALTH_PRIVATE_FIELDS", private)
        for mod in ("server", "privacy", "tools.health", "tools.measurements", "tools.workouts"):
            sys.modules.pop(mod, None)
        import server

        return server

    return _make


async def test_every_registered_tool_has_a_declared_privacy_boundary(make_server):
    server = make_server("")
    async with Client(server.mcp) as client:
        registered = {t.name for t in await client.list_tools()}
    assert registered == set(PRIVACY_BOUNDARY), (
        f"unclassified tools: {sorted(registered - set(PRIVACY_BOUNDARY))}; "
        f"stale entries: {sorted(set(PRIVACY_BOUNDARY) - registered)}"
    )


@pytest.mark.parametrize("private", sorted(SENTINELS))
async def test_private_value_never_appears_in_any_tool_resource_file_or_log(make_server, tmp_path, caplog, private):
    server = make_server(private)
    sentinel = SENTINELS[private]
    caplog.set_level(logging.DEBUG)
    today = date.today()
    window = {
        "start_date": (today - timedelta(days=39)).isoformat(),
        "end_date": today.isoformat(),
    }
    outputs: dict[str, str] = {}

    async with Client(server.mcp) as client:

        async def call(name: str, args: dict, label: str | None = None):
            try:
                result = await client.call_tool(name, args)
                text = json.dumps(result.structured_content, default=str)
            except ToolError as exc:
                text = f"ERROR {exc}"
            outputs[label or f"{name} {json.dumps(args, sort_keys=True)}"] = text

        # Both write paths store the private value; the daily log also feeds the projection.
        for i in range(40):
            day = (today - timedelta(days=i)).isoformat()
            await client.call_tool(
                "log_daily_metric", {"date": day, private: sentinel if private == "weight_kg" else 9137}
            )
            await client.call_tool(
                "log_daily_metric", {"date": day, "steps": 5000 + i * 37, "sleep_hours": 7.0 + (i % 4) * 0.3}
            )
        await call(
            "log_measurement", {"timestamp": f"{today.isoformat()}T08:00:00", "metric": private, "value": sentinel}
        )

        a_start, a_end = window["start_date"], (today - timedelta(days=20)).isoformat()
        b_start, b_end = (today - timedelta(days=19)).isoformat(), window["end_date"]
        for name, args in [
            ("read_health_data", window),
            ("export_health_data_csv", window),
            ("read_measurements", {}),
            ("read_measurements", {"metric": private}),
            ("aggregate_measurements", {"date": today.isoformat()}),
            ("get_metric_provenance", {"metric": private, "date": today.isoformat()}),
            ("get_metric_history", {"metric": private, **window}),
            ("get_baseline", {"metric": private}),
            ("detect_metric_anomalies", {"metric": private}),
            ("calculate_metric_trend", {"metric": private}),
            (
                "compare_metric_periods",
                {
                    "metric": private,
                    "period_a_start": a_start,
                    "period_a_end": a_end,
                    "period_b_start": b_start,
                    "period_b_end": b_end,
                },
            ),
            ("find_metric_correlation", {"metric_a": private, "metric_b": "steps"}),
            ("find_metric_correlation", {"metric_a": "steps", "metric_b": private}),
            ("get_recent_changes", {"days": 30}),
            ("explain_metric_change", {"metric": "steps", "date": today.isoformat()}),
            ("explain_metric_change", {"metric": private, "date": today.isoformat()}),
            ("get_baseline", {"metric": "steps"}),
            ("clear_metric", {"date": today.isoformat(), "field": private}),
            ("get_data_status", {}),
            ("get_import_status", {}),
        ]:
            await call(name, args)

        for uri in ("health://metrics/schema", f"health://day/{today.isoformat()}"):
            try:
                resource = await client.read_resource(uri)
                outputs[uri] = resource[0].text
            except Exception as exc:  # noqa: BLE001 -- an error message is an output path too
                outputs[uri] = f"ERROR {exc}"

    for csv_file in Path(tmp_path).glob("exports/*.csv"):
        outputs[f"csv file {csv_file.name}"] = csv_file.read_text(encoding="utf-8")
    outputs["server logs"] = caplog.text

    assert any(k.startswith("csv file") for k in outputs), (
        "export did not write a file; the sweep proved nothing for CSV"
    )
    leaks = {k: h for k, text in outputs.items() if (h := _hits(sentinel, text))}
    assert not leaks, json.dumps(leaks, indent=1)


async def test_private_metric_is_still_stored_and_other_metrics_unaffected(make_server):
    """Guards the sweep against passing vacuously: the sentinel really is in the
    database and non-private metrics are still returned."""
    server = make_server("weight_kg")
    today = date.today().isoformat()
    async with Client(server.mcp) as client:
        await client.call_tool("log_daily_metric", {"date": today, "weight_kg": 77.3173, "steps": 4321})
        rows = (
            await client.call_tool("read_health_data", {"start_date": today, "end_date": today})
        ).structured_content["rows"]
    assert rows[0]["steps"] == 4321
    assert rows[0]["weight_kg"] is None
    import sqlite3

    conn = sqlite3.connect(server.HEALTH_DB_PATH)
    try:
        assert conn.execute("SELECT value FROM daily_metrics WHERE metric = 'weight_kg'").fetchone() == (77.3173,)
    finally:
        conn.close()


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT: read_workout_sessions / log_workout_session ignore HEALTH_PRIVATE_FIELDS. With "
    "workout_minutes private, a session's duration_minutes (the same quantity) is still returned, and "
    "avg/max heart rate come back even with heart_rate private. Needs a policy decision: redact the "
    "fields (duration_minutes would have to become nullable in the public schema) or refuse the tool.",
)
async def test_workout_sessions_respect_private_workout_minutes(make_server):
    server = make_server("workout_minutes")
    async with Client(server.mcp) as client:
        await client.call_tool(
            "log_workout_session",
            {"date": "2026-10-01", "activity_type": "run", "duration_minutes": 137},
        )
        try:
            text = json.dumps((await client.call_tool("read_workout_sessions", {})).structured_content)
        except ToolError as exc:
            text = f"ERROR {exc}"
    assert not _hits(137, text)


@pytest.mark.parametrize("private", ["weight_kg", "water_ml"])
async def test_composites_never_surface_a_private_metric_by_name(make_server, private):
    """A value-based sweep cannot see a shape leak: a correlation, trend or
    change note computed from a private metric exposes its name, r and n without
    ever printing a raw value. Make the private series vary in lockstep with
    steps so it WOULD be the strongest correlate if composites included it."""
    server = make_server(private)
    today = date.today()
    async with Client(server.mcp) as client:
        for i in range(60):
            day = (today - timedelta(days=i)).isoformat()
            steps = 5000 + i * 37 + (i % 7) * 400
            private_value = 60.0 + steps / 1000 if private == "weight_kg" else 1000 + steps // 2
            await client.call_tool("log_daily_metric", {"date": day, "steps": steps, private: private_value})
        # A drop on the last day so get_recent_changes has something to report.
        await client.call_tool("log_daily_metric", {"date": today.isoformat(), "steps": 900})
        explained = await client.call_tool("explain_metric_change", {"metric": "steps", "date": today.isoformat()})
        recent = await client.call_tool("get_recent_changes", {"days": 14})
    for name, result in (("explain_metric_change", explained), ("get_recent_changes", recent)):
        text = json.dumps(result.structured_content, default=str)
        assert "steps" in text, f"{name} returned nothing about steps; the test proved nothing"
        assert private not in text, f"{name} surfaced the private metric {private!r}"
